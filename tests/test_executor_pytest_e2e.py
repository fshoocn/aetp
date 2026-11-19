"""pytest 执行器全链路 / 跨机集成测试（executor_pytest DESIGN §16.2）。

覆盖（分组 TestCase，对齐任务清单 5 组）：

1. **三件套自环**（同 Context：TestCorePlugin + ExecPytestMasterPlugin +
   PytestExecutorPlugin，comm 本地自环）：上传 → collect inspect（material_case
   清单）→ save_selection → 管理端预推（selections 随元包同步）→ submit（plan
   分片）→ plan/分片真 pytest 子进程执行 → get_result 计数聚合、job.log 入库
   （op = task_id）、产物落 core artifact 表；
2. **非平铺组装**：配方 layout 铺排（fixtures 的 src/cases + conf 分离）后经
   selection 展开执行成功（含依赖根 conftest greeting fixture 的用例）；
3. **inspect 全链路**：上传 → analyze（deps.missing warning 不阻塞）→ collect
   （用例数 / markers / params）→ core cases 可查；
4. **双节点跨机**（master Context + slave Context + 真实 HTTP comm，照
   tests/test_heartbeat_server.py 双节点手法，FakeNodes 节点名对齐 comm）：
   材料预推跨机、inspect 跨机应答、submit → job 跨机派发 → 结果 / 日志流跨机
   回传（含真实网络路径断言）；
5. **取消跨机**（可选）：cancel 下行杀进程树（D3），canceled 结果回传收口。

测试引导：IsolatedAsyncioTestCase + 临时 config.ini 起栈；核心派发循环挂起改
手动 ``_dispatch_tick``（杜绝并发双派）；执行跑真 pytest 子进程（venv 同栈
解释器）；杀树后以 tasklist CSV 探测残留 PID（teardown 兜底清杀防泄漏）。
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import tempfile
import time
import unittest
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast
from unittest import mock

from cordis_port import Context

from _helpers import free_port as _free_port
from common.cordis_utils import unload_all, wait_until
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.comm import CommService
from common.plugins.db import DbPlugin
from common.plugins.webapi import WebApiPlugin
from common.test_contracts import ExecutorSelector, ShardSpec, StageSpec, TaskSpec
from master_plugins.executor_pytest.plugin import ExecPytestMasterPlugin
from master_plugins.test_core import TestCorePlugin as CorePlugin
from slave_plugins.executor_pytest.plugin import PytestExecutorPlugin

#: 夹具工程（DESIGN §16.2 非平铺包：src/cases + conf 分离）
FIXTURE_DIR: Path = Path(__file__).resolve().parent / "fixtures" / "pytest_project"

#: 夹具的组装配方：铺排后 cases/ + 根 conftest.py + 根 pytest.ini 才可跑
FIXTURE_RECIPE: dict[str, Any] = {
    "layout": [
        {"src": "src/cases", "dst": "cases"},
        {"src": "conf/conftest.py", "dst": "conftest.py"},
        {"src": "conf/pytest.ini", "dst": "pytest.ini"},
    ],
    "entry": "cases",
    "args": ["-q"],
}

#: 任务终态（驱动循环出口）
TASK_FINISHED: frozenset[str] = frozenset({"succeeded", "failed", "canceled", "error"})


# ================================================================ 纯函数辅助
def fixture_zip(*, include_slow: bool = True) -> bytes:
    """tests/fixtures/pytest_project 打包（排除 __pycache__；可去掉 case_slow）。

    ``include_slow=False`` 去掉 120 秒睡眠的慢用例文件——plan 分片会按文件级
    派发全材料，含慢用例会把执行链拖到分钟级。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for path in sorted(FIXTURE_DIR.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(FIXTURE_DIR).as_posix()
            if not include_slow and rel == "src/cases/case_slow.py":
                continue
            package.write(path, rel)
    return buffer.getvalue()


def _cap(
    node_id: str,
    cap_id: str = "pytest",
    *,
    kind: str = "executor",
    labels: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """能力信封条目（FakeNodes 数据源；条目带 node_id，同 NodesInterface）。"""
    return {
        "node_id": node_id,
        "kind": kind,
        "id": cap_id,
        "provider": f"{kind}-{cap_id}",
        "version": "1.0.0",
        "labels": dict(labels or {}),
    }


def _stage(
    *,
    params: Mapping[str, Any] | None = None,
    shard_mode: Literal["none", "plan"] = "none",
    shard_hint: int = 0,
    timeout_s: float = 0.0,
) -> StageSpec:
    return StageSpec(
        name="unit",
        executor=ExecutorSelector(id="pytest"),
        params=dict(params or {}),
        shard=ShardSpec(mode=shard_mode, hint=shard_hint),
        timeout_s=timeout_s,
    )


def _spec(
    params: Mapping[str, Any],
    *,
    name: str = "pytest 全链路",
    shard_mode: Literal["none", "plan"] = "none",
    shard_hint: int = 0,
    timeout_s: float = 0.0,
) -> TaskSpec:
    return TaskSpec(
        name=name,
        stages=[_stage(params=params, shard_mode=shard_mode, shard_hint=shard_hint, timeout_s=timeout_s)],
    )


def _pid_alive(pid: int) -> bool:
    """Windows 进程存活探测（tasklist CSV 形态：整字段精确匹配 PID）。"""
    try:
        out = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return f'"{pid}"' in out.stdout


def _ensure_dead(pid: int) -> None:
    """兜底清杀残留子进程（teardown 防泄漏；正常路径已被杀树覆盖）。"""
    if not _pid_alive(pid):
        return
    subprocess.run(  # noqa: S603
        ["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S607
        capture_output=True,
        timeout=15,
    )


class FakeNodes:
    """假节点目录（NodesInterface）：kinds / labels 子集匹配，条目带 node_id。"""

    def __init__(self, caps: Sequence[Mapping[str, object]] = ()) -> None:
        self.caps: list[dict[str, object]] = [dict(entry) for entry in caps]

    def list(self, *, online_only: bool = False) -> list[dict[str, str]]:
        return [
            {
                "node_id": str(entry.get("node_id") or ""),
                "node_kind": "slave",
                "address": "",
                "status": "online",
            }
            for entry in self.caps
        ]

    def select(
        self,
        *,
        kinds: Sequence[str] | None = None,
        labels: Mapping[str, str] | None = None,
        online_only: bool = True,
    ) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for entry in self.caps:
            if kinds is not None and entry.get("kind") not in set(kinds):
                continue
            entry_labels = cast(Mapping[str, str], entry.get("labels") or {})
            if labels is not None and not all(
                entry_labels.get(key) == value for key, value in dict(labels).items()
            ):
                continue
            out.append(dict(entry))
        return out


class _LogStub:
    """log 服务桩：记录 ``ingest_remote`` 的 (payload, meta)（入库映射断言用）。"""

    def __init__(self) -> None:
        self.batches: list[tuple[dict[str, Any], Mapping[str, Any]]] = []

    def ingest_remote(self, payload: object, meta: object) -> None:
        self.batches.append(
            (dict(cast(Mapping[str, Any], payload)), cast(Mapping[str, Any], meta))
        )

    def entries(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for payload, _meta in self.batches:
            rows.extend(
                dict(cast(Mapping[str, Any], item)) for item in payload.get("logs", [])
            )
        return rows


class ExecutorChainTestCase(unittest.IsolatedAsyncioTestCase):
    """全链路共用驱动：上传 / inspect 等待 / 派发驱动（子类按拓扑装配栈）。"""

    context: Context | None
    core: Any
    master: Any
    log_stub: _LogStub

    async def asyncSetUp(self) -> None:
        # 核心派发循环挂起：测试手动 _dispatch_tick 驱动，杜绝并发双派
        self._interval_patch = mock.patch(
            "master_plugins.test_core.plugin._DISPATCH_INTERVAL", 3600.0
        )
        self._interval_patch.start()
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.log_stub = _LogStub()
        self.context = None
        self.core = None
        self.master = None
        self._stray_pids: list[int] = []

    async def asyncTearDown(self) -> None:
        await self.teardown_contexts()
        self._interval_patch.stop()
        for pid in self._stray_pids:
            _ensure_dead(pid)
        self._temp.cleanup()

    async def teardown_contexts(self) -> None:
        """卸载本用例装配的全部 Context（子类可覆写排序）。"""
        if self.context is not None:
            await unload_all(self.context)
            self.context = None

    # ------------------------------------------------------------ 驱动辅助
    async def upload_material(
        self,
        *,
        name: str = "链路包",
        include_slow: bool = False,
        data: bytes | None = None,
    ) -> dict[str, Any]:
        payload = data if data is not None else fixture_zip(include_slow=include_slow)
        result = await self.core.upload_material(
            name=name, data=payload, recipe=dict(FIXTURE_RECIPE)
        )
        return dict(cast(Mapping[str, Any], result))

    async def run_inspect(
        self, material_id: str, mode: str, *, timeout: float = 120.0
    ) -> Mapping[str, Any]:
        """触发 inspect 并等待应答落库收尾（族内通道，返回记录快照）。"""
        request_id = await self.master.inspect(material_id, mode)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = await self.master.inspect_status(request_id)
            if status is not None and status["state"] != "inspecting":
                return status
            await asyncio.sleep(0.1)
        self.fail(f"inspect {request_id}（{mode}）超时未完成")

    async def drive_task(self, task_id: str, *, timeout: float = 240.0) -> str:
        """手动派发驱动直到任务终态；返回终态。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self.core._dispatch_tick()
            detail = await self.core.get_task(task_id)
            assert detail is not None
            state = str(detail["state"])
            if state in TASK_FINISHED:
                return state
            await asyncio.sleep(0.15)
        detail = await self.core.get_task(task_id)
        self.fail(f"任务 {task_id} 超时未终结：{detail}")


# ================================================================ 1-3. 三件套自环
class SelfLoopFullChainTests(ExecutorChainTestCase):
    """三件套同 Context + comm 自环：预推 / plan 分片 / selection / inspect 全链路。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.exec: Any = None

    async def boot(self) -> None:
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\n"
            "kind = master\n"
            "name = node-a\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'aetp.db').as_posix()}\n"
            "\n[comm]\n"
            "batch_interval = 0.05\n",
            encoding="utf-8",
        )
        context = Context()
        self.context = context
        context.provide("node_kind", "master")
        context.provide("node_name", "node-a")
        await context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await context.plugin(DbPlugin)
        await context.plugin(CommService)
        context.provide("nodes", FakeNodes([_cap("node-a")]))
        context.provide("log", self.log_stub)
        await context.plugin(CorePlugin)
        await context.plugin(ExecPytestMasterPlugin)
        await context.plugin(PytestExecutorPlugin, {"shard_files": 1})
        self.core = context.test_core
        self.master = context.executor_pytest_master
        self.exec = context.executor_pytest

    async def wait_material_ready(self, material_id: str) -> None:
        ok = await wait_until(
            lambda: self.exec._materials.is_ready(material_id), timeout=30.0, interval=0.05
        )
        self.assertTrue(ok, f"执行端材料 {material_id} 未就绪")

    # ------------------------------------------------------------ 1. 三件套自环
    async def test_three_piece_self_loop_plan_shard_full_chain(self) -> None:
        await self.boot()
        material = await self.upload_material()  # 11 用例（无 case_slow）
        material_id = str(material["id"])
        # 预推前置（§5.2）：inspect / 下发都要求材料先到执行端
        first_push = await self.master.push_material(material_id)
        self.assertEqual(first_push, {"pushed": ["node-a"], "failed": []})
        await self.wait_material_ready(material_id)
        # collect inspect（族内通道自环）→ material_case 清单落核心
        collect = await self.run_inspect(material_id, "collect")
        self.assertTrue(collect["ok"])
        cases = await self.core.list_material_cases(material_id)
        self.assertEqual(len(cases), 11)
        case_ids = [str(row["case_id"]) for row in cases]
        self.assertIn("cases/case_basic.py::test_pass", case_ids)
        # save_selection → 带 selections 再推（元包同步选择集到执行端）
        selection_id = await self.core.save_selection(material_id, "全部快用例", case_ids)
        pushed = await self.master.push_material(material_id, selection_ids=[selection_id])
        self.assertEqual(pushed, {"pushed": ["node-a"], "failed": []})
        # submit → plan 分片 → 分片执行 → 结果聚合
        task_id = await self.core.submit(
            _spec(
                {"material_id": material_id, "selection_id": selection_id},
                shard_mode="plan",
                shard_hint=2,
            )
        )
        state = await self.drive_task(task_id)
        self.assertEqual(state, "failed")  # 夹具含故意失败用例 test_fail
        jobs = await self.core.list_jobs(task_id)
        self.assertEqual(len(jobs), 3)  # 1 个 plan job + 2 个文件级分片 job
        shards = sorted(
            (
                int(cast(Mapping[str, Any], row["shard"])["index"])
                for row in jobs
                if isinstance(row.get("shard"), Mapping) and not row["shard"].get("plan")
            )
        )
        self.assertEqual(shards, [0, 1])
        # get_result 计数聚合（plan 的 total 与分片计数求和口径）
        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(
            result["counts"],
            {
                "total": 22,  # plan 11 + 分片 6 + 5
                "passed": 7,
                "failed": 1,
                "errors": 0,
                "skipped": 1,
                "xfailed": 1,
                "xpassed": 1,
            },
        )
        self.assertEqual(
            result["cases"],
            {"passed": 7, "failed": 1, "skipped": 1, "xfailed": 1, "xpassed": 1},
        )
        # material_case 清单（执行端 collect 物化口径，核心可查）
        rows = await self.core.list_material_cases(material_id)
        self.assertEqual({str(row["case_id"]) for row in rows}, set(case_ids))
        # job.log 已入库：op = task_id（追踪元组口径）
        entries = self.log_stub.entries()
        self.assertTrue(entries, "job.log 未入库")
        self.assertTrue(all(str(entry["op"]) == task_id for entry in entries))
        # 产物落 core artifact 表（junit.xml × 2 分片 + pytest.log）
        artifacts = await self.core.list_artifacts(task_id=task_id)
        names = [str(row["name"]) for row in artifacts]
        self.assertEqual(set(names), {"junit.xml", "pytest.log"})
        self.assertEqual(names.count("junit.xml"), 2)  # 每个分片 job 一份报告
        for row in artifacts:
            self.assertTrue(str(row["uri"]).startswith("local:///"))
            self.assertGreater(int(row["size"]), 0)
            self.assertEqual(len(str(row["sha256"])), 64)

    # ------------------------------------------------------------ 2. 非平铺组装
    async def test_non_flat_layout_assembles_and_selection_runs_green(self) -> None:
        await self.boot()
        material = await self.upload_material()
        material_id = str(material["id"])
        pushed = await self.master.push_material(material_id)  # 预推前置（§5.2）
        self.assertEqual(pushed["pushed"], ["node-a"])
        await self.wait_material_ready(material_id)
        await self.run_inspect(material_id, "collect")
        rows = await self.core.list_material_cases(material_id)
        by_id = {str(row["case_id"]) for row in rows}
        # test_pass 依赖 conf/conftest.py 铺排接线的 greeting fixture；
        # 参数化用例走 case_params——selection 展开为目标（§20-Q1 (a)）
        picked = sorted(
            case_id
            for case_id in by_id
            if case_id == "cases/case_basic.py::test_pass"
            or case_id.startswith("cases/case_params.py::")
        )
        self.assertEqual(len(picked), 6)
        selection_id = await self.core.save_selection(material_id, "绿用例", picked)
        pushed = await self.master.push_material(material_id, selection_ids=[selection_id])
        self.assertEqual(pushed["pushed"], ["node-a"])
        task_id = await self.core.submit(
            _spec({"material_id": material_id, "selection_id": selection_id})
        )
        state = await self.drive_task(task_id)
        self.assertEqual(state, "succeeded")  # layout 铺排 + conftest 接线后全绿
        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(
            result["counts"],
            {
                "total": 6,
                "passed": 6,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
            },
        )
        ran = await self.core.list_case_results(task_id=task_id)
        self.assertEqual({str(row["case_id"]) for row in ran}, set(picked))

    # ------------------------------------------------------------ 3. inspect 全链路
    async def test_inspect_full_chain_analyze_then_collect(self) -> None:
        await self.boot()
        material = await self.upload_material(include_slow=True, name="全量包")
        material_id = str(material["id"])
        pushed = await self.master.push_material(material_id)  # 预推前置（§5.2）
        self.assertEqual(pushed["pushed"], ["node-a"])
        await self.wait_material_ready(material_id)
        # analyze：deps.missing 仅 warning 不阻塞（夹具故意声明缺失依赖）
        analyze = await self.run_inspect(material_id, "analyze")
        self.assertTrue(analyze["ok"])
        problems = cast(list[Mapping[str, Any]], analyze["problems"])
        codes = [(str(item["code"]), str(item["severity"])) for item in problems]
        self.assertIn(("deps.missing", "warning"), codes)
        self.assertEqual([code for code, severity in codes if severity == "error"], [])
        dep = next(item for item in problems if item["code"] == "deps.missing")
        self.assertIn("aetp-missing-probe-dep", str(dep["message"]))
        self.assertEqual(dep["path"], "requirements.txt")
        # 落库：state=analyzed（ok 且无 error 级问题）+ problems 全量入行
        row = await self.core.get_material(material_id)
        assert row is not None
        self.assertEqual(row["state"], "analyzed")
        self.assertEqual(len(cast(list[object], row["problems"])), len(problems))
        # collect：13 用例结构化清单（6 基础 + 5 参数化 + 2 慢用例）
        collect = await self.run_inspect(material_id, "collect")
        self.assertTrue(collect["ok"])
        self.assertEqual(collect["problems"], [])
        cases = await self.core.list_material_cases(material_id)
        self.assertEqual(len(cases), 13)
        by_id = {str(item["case_id"]): item for item in cases}
        self.assertEqual(by_id["cases/case_basic.py::test_skip"]["markers"], ["skip"])
        self.assertEqual(
            by_id["cases/case_params.py::test_tagged[t-smoke]"]["params"], {"id": "t-smoke"}
        )
        self.assertEqual(by_id["cases/case_slow.py::test_sleep"]["params"], {})


# ================================================================ 4-5. 双节点跨机
class CrossMachineTests(ExecutorChainTestCase):
    """master + slave 双 Context、真实 HTTP comm（test_heartbeat_server 手法）。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.master_ctx: Context | None = None
        self.slave_ctx: Context | None = None
        self.slave_exec: Any = None
        self.slave_root: Path = self.root / "slave"

    async def teardown_contexts(self) -> None:
        for ctx in (self.slave_ctx, self.master_ctx):
            if ctx is not None:
                await unload_all(ctx)
        self.slave_ctx = None
        self.master_ctx = None
        self.context = None

    async def boot_pair(self) -> None:
        # ---- 主节点：汇聚点（webapi 挂 comm ingest/poll/ack + 核心 + 管理端）
        master_port = _free_port()
        master_config = self.root / "master" / "config.ini"
        master_config.parent.mkdir(parents=True, exist_ok=True)
        master_config.write_text(
            "[node]\nkind = master\nname = master-1\n\n[web]\n"
            f"port = {master_port}\n"
            "auth_token = test-token\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'master' / 'aetp.db').as_posix()}\n"
            "\n[comm]\n"
            "batch_interval = 0.05\n",
            encoding="utf-8",
        )
        master_ctx = Context()
        self.master_ctx = master_ctx
        self.context = master_ctx  # teardown 兜底（teardown_contexts 优先走 pair）
        master_ctx.provide("node_kind", "master")
        master_ctx.provide("node_name", "master-1")
        await master_ctx.plugin(AppConfigPlugin, {"path": str(master_config)})
        await master_ctx.plugin(DbPlugin)
        await master_ctx.plugin(WebApiPlugin)
        await master_ctx.plugin(CommService)
        master_ctx.provide("nodes", FakeNodes([_cap("slave-1")]))
        master_ctx.provide("log", self.log_stub)
        await master_ctx.plugin(CorePlugin)
        await master_ctx.plugin(ExecPytestMasterPlugin)
        self.core = master_ctx.test_core
        self.master = master_ctx.executor_pytest_master
        # ---- 从节点：真实 HTTP 上行 / 下行（target 指向主节点）
        slave_port = _free_port()
        slave_config = self.slave_root / "config.ini"
        slave_config.parent.mkdir(parents=True, exist_ok=True)
        slave_config.write_text(
            "[node]\nkind = slave\nname = slave-1\n\n[web]\n"
            f"port = {slave_port}\n"
            "auth_token = test-token\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.slave_root / 'aetp.db').as_posix()}\n"
            "\n[comm]\n"
            f"target = http://127.0.0.1:{master_port}\n"
            "batch_interval = 0.05\n",
            encoding="utf-8",
        )
        slave_ctx = Context()
        self.slave_ctx = slave_ctx
        slave_ctx.provide("node_kind", "slave")
        slave_ctx.provide("node_name", "slave-1")
        await slave_ctx.plugin(AppConfigPlugin, {"path": str(slave_config)})
        await slave_ctx.plugin(DbPlugin)
        await slave_ctx.plugin(WebApiPlugin)
        await slave_ctx.plugin(CommService)
        await slave_ctx.plugin(PytestExecutorPlugin, {"material_wait_s": 30.0})
        self.slave_exec = slave_ctx.executor_pytest

    async def wait_slave_material_ready(self, material_id: str) -> None:
        ok = await wait_until(
            lambda: self.slave_exec._materials.is_ready(material_id),
            timeout=30.0,
            interval=0.1,
        )
        self.assertTrue(ok, f"跨机预推的材料 {material_id} 未在从端就绪")

    # ------------------------------------------------------------ 4. 跨机全链路
    async def test_cross_machine_push_inspect_submit_and_log_return(self) -> None:
        await self.boot_pair()
        material = await self.upload_material()  # 11 用例（无 case_slow）
        material_id = str(material["id"])
        # 材料预推跨机：主端分块下行 → HTTP poll → 从端 MaterialStore 拼装就绪
        pushed = await self.master.push_material(material_id)
        self.assertEqual(pushed, {"pushed": ["slave-1"], "failed": []})
        await self.wait_slave_material_ready(material_id)
        # inspect 跨机：请求下行、应答上行（node_id 是从端名）
        collect = await self.run_inspect(material_id, "collect")
        self.assertTrue(collect["ok"])
        self.assertEqual(collect["node_id"], "slave-1")
        cases = await self.core.list_material_cases(material_id)
        self.assertEqual(len(cases), 11)
        # submit → job 跨机派发 → 真 pytest 执行 → 结果 / 日志流跨机回传
        task_id = await self.core.submit(
            _spec({"material_id": material_id, "targets": ["cases/case_params.py"]})
        )
        state = await self.drive_task(task_id)
        self.assertEqual(state, "succeeded")
        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(
            result["counts"],
            {
                "total": 5,
                "passed": 5,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
            },
        )
        self.assertEqual(result["jobs"][0]["node_id"], "slave-1")
        # 日志流跨机回传入库：op = task_id，收件 meta 溯源从节点
        entries = self.log_stub.entries()
        self.assertTrue(entries, "job.log 未跨机回传")
        self.assertTrue(all(str(entry["op"]) == task_id for entry in entries))
        self.assertTrue(
            all(meta.get("node") == "slave-1" for _payload, meta in self.log_stub.batches)
        )
        # 真实网络路径断言：汇聚点节点表只经 HTTP ingest/poll 注册（本地自环
        # 不写该表），从端执行计数只存在于对端 Context
        master_comm = cast(Any, self.master_ctx).comm
        self.assertIn("slave-1", master_comm.nodes())
        self.assertGreaterEqual(self.slave_exec.stats()["executed"], 1)

    # ------------------------------------------------------------ 5. 取消跨机
    async def test_cross_machine_cancel_kills_process_tree(self) -> None:
        await self.boot_pair()
        material = await self.upload_material(include_slow=True, name="慢用例包")
        material_id = str(material["id"])
        pushed = await self.master.push_material(material_id)
        self.assertEqual(pushed["pushed"], ["slave-1"])
        await self.wait_slave_material_ready(material_id)
        task_id = await self.core.submit(
            _spec(
                {
                    "material_id": material_id,
                    "targets": ["cases/case_slow.py::test_sleep_with_child"],
                },
                timeout_s=120.0,
            )
        )
        # 等慢用例子进程落 PID（cwd = 从端组装目录）；等待期间手动驱派发
        deadline = time.monotonic() + 60.0
        child_file: Path | None = None
        while time.monotonic() < deadline:
            await self.core._dispatch_tick()
            jobs = await self.core.list_jobs(task_id)
            if jobs:
                job_id = str(jobs[0]["job_id"])
                child_file = self.slave_root / "workdir" / job_id / "a1" / "child_pid.txt"
                if child_file.is_file():
                    break
            await asyncio.sleep(0.1)
        self.assertIsNotNone(child_file, "job 未派发")
        assert child_file is not None
        self.assertTrue(child_file.is_file(), "慢用例子进程未落 child_pid.txt")
        pid = int(child_file.read_text(encoding="utf-8").strip())
        self._stray_pids.append(pid)
        self.assertTrue(_pid_alive(pid))
        # cancel 下行（真实 HTTP）→ 从端杀进程树 → canceled 结果回传收口
        self.assertTrue(await self.core.cancel(task_id, "测试取消"))
        state = await self.drive_task(task_id)
        self.assertEqual(state, "canceled")
        rows = await self.core.list_jobs(task_id)
        self.assertEqual(rows[0]["state"], "canceled")
        ok = await wait_until(lambda: not _pid_alive(pid), timeout=15.0, interval=0.2)
        self.assertTrue(ok, f"子进程 {pid} 未被杀树清理（D3）")
        self.assertGreaterEqual(self.slave_exec.stats()["canceled"], 1)


if __name__ == "__main__":
    unittest.main()
