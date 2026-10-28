"""事件派发模式契约门禁（GAP-2 机器化；Phase 0.6 / 设计文档附录 G #3）。

扫描 ``common/``、``master_plugins/``、``slave_plugins/`` 的 cordis 派发点：

* 可解析派发（事件名为字面量或模块级字符串常量）必须在
  :mod:`common.event_contract` 声明，且声明的 mode 与派发方法一致；
* ``emit`` 声明的事件不得被仓库内可见的 ``async def`` 监听器注册——
  ``emit`` 会**静默丢弃** async 监听器（实测 E2），这是 GAP-2 的机器防线。

动态派发点（首参是变量，如 comm 的 topic 派发、心跳的 ``_broadcast``）
不在扫描范围——新增那类事件时**人工**对照注册表声明。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from cordis_port import Context

from common.cordis_utils import parallel_event
from common.event_contract import EVENTS, EventMode, declare_event, event_mode

_ROOTS: tuple[str, ...] = ("common", "master_plugins", "slave_plugins")
_DISPATCH_METHODS: dict[str, EventMode] = {
    "emit": "emit",
    "parallel": "parallel",
    "serial": "serial",
    "bail": "bail",
    "waterfall": "waterfall",
}
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """模块级字符串常量（如 ``STATUS_EVENT = "heartbeat/collect-status"``）。"""
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            value = node.value.value
            if isinstance(value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = value
    return consts


def _event_name(node: ast.expr, consts: dict[str, str]) -> str | None:
    """解析事件名实参：字面量或模块常量；其余（变量）返回 None 表示跳过。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _iter_sources() -> list[tuple[Path, ast.Module]]:
    sources: list[tuple[Path, ast.Module]] = []
    for root in _ROOTS:
        for path in sorted((_REPO_ROOT / root).rglob("*.py")):
            sources.append(
                (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            )
    return sources


def scan_dispatches() -> list[tuple[str, int, str, str]]:
    """全部可解析派发点：``(文件, 行号, 事件名, 派发方法)``。"""
    findings: list[tuple[str, int, str, str]] = []
    for path, tree in _iter_sources():
        consts = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            mode: str | None = None
            name_arg: ast.expr | None = None
            if isinstance(func, ast.Attribute) and func.attr in _DISPATCH_METHODS:
                mode = _DISPATCH_METHODS[func.attr]
                name_arg = node.args[0] if node.args else None
            elif isinstance(func, ast.Name) and func.id == "parallel_event":
                mode = "parallel"
                name_arg = node.args[1] if len(node.args) > 1 else None
            if mode is None or name_arg is None:
                continue
            name = _event_name(name_arg, consts)
            if name is None or name.startswith("internal/"):
                continue
            findings.append((path.name, node.lineno, name, mode))
    return findings


def scan_async_listeners() -> list[tuple[str, int, str]]:
    """在 ``emit`` 声明事件上注册仓库内 ``async def`` 监听器的挂点。

    近似解析：监听器是同模块的异步函数名，或 ``self._x`` 形态且同模块任意
    类存在同名异步方法。闭包 / 跨模块监听器不在解析范围。
    """
    findings: list[tuple[str, int, str]] = []
    for path, tree in _iter_sources():
        consts = _module_constants(tree)
        async_names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef):
                async_names.add(node.name)
            elif isinstance(node, ast.ClassDef):
                async_names.update(
                    item.name for item in node.body if isinstance(item, ast.AsyncFunctionDef)
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "on" or len(node.args) < 2:
                continue
            name = _event_name(node.args[0], consts)
            if name is None or event_mode(name) != "emit":
                continue
            listener = node.args[1]
            hit = False
            if isinstance(listener, ast.Name):
                hit = listener.id in async_names
            elif isinstance(listener, ast.Attribute):
                hit = listener.attr in async_names
            if hit:
                findings.append((path.name, node.lineno, name))
    return findings


class EventContractTests(unittest.TestCase):
    """注册表自检 + 派发点一致性门禁。"""

    def test_registry_is_wellformed(self) -> None:
        for spec in EVENTS.values():
            self.assertEqual(spec.name in EVENTS, True)
            self.assertTrue(spec.summary, f"{spec.name} 缺 summary")
            self.assertIn(
                spec.mode,
                ("emit", "waterfall", "parallel", "serial", "bail"),
            )

    def test_declare_rejects_conflicting_redeclaration(self) -> None:
        declare_event("test/contract-probe", "parallel", "探测事件")
        self.assertEqual(event_mode("test/contract-probe"), "parallel")
        with self.assertRaisesRegex(ValueError, "重复声明"):
            declare_event("test/contract-probe", "emit", "改了模式")

    def test_dispatch_sites_match_declared_mode(self) -> None:
        for path, lineno, name, mode in scan_dispatches():
            with self.subTest(event=name, file=path):
                declared = event_mode(name)
                self.assertIsNotNone(declared, f"{path}:{lineno} 派发了未声明事件 {name!r}")
                self.assertEqual(
                    declared,
                    mode,
                    f"{path}:{lineno} 以 {mode} 派发 {name!r}，声明为 {declared}",
                )

    def test_no_async_listener_on_emit_events(self) -> None:
        findings = scan_async_listeners()
        self.assertEqual(
            findings,
            [],
            "emit 事件被 async def 监听器订阅会静默失效（GAP-2）：改 parallel 派发"
            "或把监听器改为同步派生任务",
        )


class ParallelEventTests(unittest.IsolatedAsyncioTestCase):
    """``parallel_event``：异常隔离并返回错误列表，不吞不抛。"""

    async def test_collects_listener_errors_without_raising(self) -> None:
        ctx = Context()
        seen: list[object] = []

        def ok(payload: object) -> None:
            seen.append(payload)

        def boom(_payload: object) -> None:
            raise ValueError("listener broken")

        async def async_ok(payload: object) -> None:
            seen.append(("async", payload))

        ctx.on("test/evt", ok)
        ctx.on("test/evt", boom)
        ctx.on("test/evt", async_ok)
        errors = await parallel_event(ctx, "test/evt", {"n": 1})
        self.assertEqual(len(errors), 1)
        self.assertIn("listener broken", str(errors[0]))
        self.assertIn({"n": 1}, seen)  # 其余监听器照常执行（含 async）
        self.assertIn(("async", {"n": 1}), seen)

    async def test_returns_empty_list_on_success(self) -> None:
        ctx = Context()
        ctx.on("test/ok", lambda _payload: None)
        self.assertEqual(await parallel_event(ctx, "test/ok", {}), [])


if __name__ == "__main__":
    unittest.main()
