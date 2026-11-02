"""用户自定义流程（Phase 5 / §5.14）测试：版本化保存 / 覆盖语义端到端 /
模板升级重校验 / 导入导出 / 附加项 / HTTP 路由 / 页面注册。

§7.5 验收口径：**用户无需改代码即可关闭可选节点、插入步骤、切换实现并保存
为新版本；模板升级后用户流程自动重新校验**。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cordis_port import Context

from _helpers import AUTH_TOKEN, arequest, free_port
from common.cordis_utils import unload_all
from common.flow_contracts import (
    FlowDef,
    FlowError,
    FlowRejected,
    NodeDef,
    SlotDef,
    StepContext,
    StepOutput,
)
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.db import DbPlugin
from common.plugins.flow import FlowService
from common.plugins.flow.service import USER_FLOW_SCHEMA

#: 附录 C 风格模板：锁定节点 + 可选节点 + 双实现 + 插槽
TEMPLATE = FlowDef(
    id="test-task",
    version=1,
    title="测试任务",
    nodes=(
        NodeDef("split", "split", locked=True),
        NodeDef("dispatch", "dispatch", locked=True),
        NodeDef("report", "report", labels={"format": "html"}, required=False),
        NodeDef("notify", "notify", required=False),
        NodeDef("persist", "persist", always=True),
    ),
    slots=(SlotDef("after-stats", after="dispatch", stages=("audit", "notify")),),
)

#: 模板升级版：report 节点被删除（引用它的覆盖将失效）
TEMPLATE_V2 = FlowDef(
    id="test-task",
    version=2,
    title="测试任务 v2",
    nodes=(
        NodeDef("split", "split", locked=True),
        NodeDef("dispatch", "dispatch", locked=True),
        NodeDef("notify", "notify", required=False),
        NodeDef("persist", "persist", always=True),
    ),
    slots=(SlotDef("after-stats", after="dispatch", stages=("audit", "notify")),),
)


class _WebUiStub:
    """最小 webui 服务桩：只记录 register_ui（真实契约由 test_web_ui 覆盖）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def register_ui(self, contribution_id: str, **options: object) -> None:
        self.calls.append((contribution_id, dict(options)))


class FlowUserTests(unittest.IsolatedAsyncioTestCase):
    """服务层：保存 / 版本 / 覆盖语义 / 重校验 / 导入导出 / 附加项。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._root = Path(self._temp.name)
        self.calls: list[str] = []
        await self.boot()

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        for _ in range(50):
            try:
                self._temp.cleanup()
                break
            except PermissionError:
                await asyncio.sleep(0.05)

    async def boot(self) -> None:
        config_path = self._root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\nname = test-node\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self._root / 'aetp.db').as_posix()}\n"
            "\n[flow]\n"
            "cancel_grace = 0.1\nretry_cap = 0.01\nstep_timeout = 5\n"
            "resolve_wait = 1\nresume_delay = 0\n",
            encoding="utf-8",
        )
        self.context = Context()
        self.context.provide("node_kind", "master")
        self.context.provide("node_name", "test-node")
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)
        await self.context.plugin(FlowService)
        self._template_dispose = self.context.flow.define(TEMPLATE)

    def handler(self, name: str):
        async def run(_sc: StepContext) -> StepOutput:
            self.calls.append(name)
            return {"by": name}

        return run

    def register_defaults(self) -> None:
        flow = self.context.flow
        flow.step("split", "split", self.handler("split"))
        flow.step("dispatch", "dispatch", self.handler("dispatch"))
        flow.step("report", "report-html", self.handler("report-html"), labels={"format": "html"})
        flow.step("notify", "notify", self.handler("notify"))
        flow.step("persist", "persist", self.handler("persist"))
        flow.step("audit", "audit", self.handler("audit"))

    async def wait_run(self, run_id: str) -> dict[str, Any]:
        for _ in range(100):
            status = await self.context.flow.status(run_id)
            assert status is not None
            if status["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
                return status
            await asyncio.sleep(0.05)
        self.fail(f"run {run_id} 未到终态")

    # ------------------------------------------------------- 版本化（不可变）
    async def test_save_generates_immutable_versions(self) -> None:
        flow = self.context.flow
        first = await flow.save_flow(
            "uf1", {"title": "我的流程", "extends": "test-task", "overrides": {"disable": ["report"]}},
            actor="alice",
        )
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["created_by"], "alice")
        second = await flow.save_flow(
            "uf1", {"extends": "test-task", "overrides": {"disable": []}}, actor="bob",
        )
        self.assertEqual(second["version"], 2)
        self.assertEqual([v["version"] for v in second["versions"]], [1, 2])
        self.assertEqual([v["created_by"] for v in second["versions"]], ["alice", "bob"])
        # 版本不可变：详情只按最新版本物化
        self.assertEqual(second["overrides"], {"disable": []})

    # --------------------------------- 覆盖语义端到端（§7.5 验收：不改代码）
    async def test_overrides_end_to_end(self) -> None:
        """关闭可选节点 + 插入步骤 + 切换实现（labels）+ 保存新版本 + 运行。"""
        self.register_defaults()
        # 双实现并存（切换实现的前提）：junit 版 report（附录 C.3 同款）
        self.context.flow.step(
            "report", "report-junit", self.handler("report-junit"), labels={"format": "junit"}
        )
        flow = self.context.flow
        detail = await flow.save_flow(
            "uf2",
            {
                "extends": "test-task",
                "overrides": {
                    "disable": ["notify"],
                    "insert": [
                        {"after": "dispatch", "node": {"id": "extra-audit", "stage": "audit"}}
                    ],
                    "labels": {"report": {"format": "junit"}},
                },
            },
            actor="alice",
        )
        self.assertEqual(detail["needs_attention"], False)
        handle = await flow.start("uf2")
        status = await self.wait_run(handle.id)
        self.assertEqual(status["state"], "succeeded")
        self.assertNotIn("notify", self.calls)  # 可选节点已关闭
        self.assertIn("audit", self.calls)  # 插槽插入的节点执行了（步骤 audit/audit）
        self.assertIn("report-junit", self.calls)  # 切换实现（labels format=junit）
        self.assertNotIn("report-html", self.calls)
        self.assertIn("persist", self.calls)
        # 单次运行覆盖叠加在持久化覆盖之上（§5.14.1 同构）：后者可关前者插的节点
        handle = await flow.start("uf2", overrides={"disable": ["extra-audit"]})
        status = await self.wait_run(handle.id)
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(self.calls.count("audit"), 1)  # 第二次运行已关闭插入节点

    # --------------------------------------------------------- 保存校验（422）
    async def test_save_rejects_locked_and_bad_slot(self) -> None:
        flow = self.context.flow
        with self.assertRaises(FlowRejected) as caught:
            await flow.save_flow(
                "uf3", {"extends": "test-task", "overrides": {"disable": ["split"]}}
            )
        codes = {p.code.value for p in caught.exception.problems}
        self.assertIn("locked", codes)  # locked 节点不可关闭（§5.14.2）

        with self.assertRaises(FlowRejected) as caught:
            await flow.save_flow(
                "uf3",
                {"extends": "test-task",
                 "overrides": {"insert": [{"after": "split", "node": {"id": "x", "stage": "notify"}}]}},
            )
        codes = {p.code.value for p in caught.exception.problems}
        self.assertIn("unknown_slot", codes)  # 没声明插槽不接受插入

        with self.assertRaises(FlowError):
            await flow.save_flow("test-task", {"def": {"id": "test-task", "nodes": []}})
        with self.assertRaises(FlowError):
            await flow.save_flow("uf3", {"extends": "ghost"})

    # --------------------------------------- 独立定义（def 形态）+ 导入导出
    async def test_standalone_def_and_import_export(self) -> None:
        self.register_defaults()
        flow = self.context.flow
        detail = await flow.save_flow(
            "uf4",
            {
                "$schema": USER_FLOW_SCHEMA,
                "def": {
                    "id": "uf4", "title": "独立流程",
                    "nodes": [
                        {"id": "n1", "stage": "split", "required": True},
                        {"id": "n2", "stage": "persist", "required": False},
                    ],
                },
            },
            actor="alice",
        )
        self.assertEqual(detail["extends"], "")
        self.assertEqual(detail["export"]["$schema"], USER_FLOW_SCHEMA)

        # 导出 → 导入为另一条流程（git / CI 场景，§5.14.4）
        exported = dict(detail["export"])
        exported["id"] = "uf4-copy"
        exported["def"] = dict(exported["def"])
        exported["def"]["id"] = "uf4-copy"
        await flow.save_flow("uf4-copy", exported, actor="ci")
        handle = await flow.start("uf4-copy")
        status = await self.wait_run(handle.id)
        self.assertEqual(status["state"], "succeeded")
        self.assertIn("split", self.calls)

    # --------------------------------------------- 模板升级自动重校验（§5.14.1）
    async def test_template_upgrade_marks_needs_attention(self) -> None:
        flow = self.context.flow
        await flow.save_flow(
            "uf5", {"extends": "test-task", "overrides": {"disable": ["report"]}}
        )
        # 模板热升级（注销旧模板 → 同 id 登记 v2，report 节点消失）→ 覆盖失效
        self._template_dispose()
        self.context.flow.define(TEMPLATE_V2)
        detail: dict[str, Any] = {}
        for _ in range(50):
            detail = await flow.flow_detail("uf5")
            if detail["needs_attention"]:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(detail["needs_attention"])  # 自动重校验标 needs_attention
        with self.assertRaisesRegex(FlowError, "重新校验"):
            await flow.start("uf5")  # 禁止新启动（已在跑的不受影响）
        # 保存修复后的新版本即可重新启用
        fixed = await flow.save_flow(
            "uf5", {"extends": "test-task", "overrides": {"disable": []}}
        )
        self.assertFalse(fixed["needs_attention"])

    # --------------------------------------------------------------- 附加项
    async def test_attachments_enable_disable(self) -> None:
        self.register_defaults()
        flow = self.context.flow
        dispose = flow.attachment(
            "test-task", "after-stats", "extra-audit", "audit", summary="追加审计",
        )
        items = flow.attachments()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["enabled_in"], [])  # 默认关闭（§5.14.3）
        await flow.save_flow("uf6", {"extends": "test-task", "overrides": {}})
        detail = await flow.set_attachment_enabled(
            "test-task:after-stats:extra-audit", "uf6", True, actor="admin"
        )
        self.assertEqual(detail["version"], 2)  # 启用写入新版本
        self.assertIn("extra-audit", [n["id"] for n in detail["effective_nodes"]])
        self.assertEqual(flow.attachments()[0]["enabled_in"], ["uf6"])
        handle = await flow.start("uf6")
        status = await self.wait_run(handle.id)
        self.assertEqual(status["state"], "succeeded")
        self.assertIn("audit", self.calls)  # 附加项插入节点执行（步骤 audit/audit）

        detail = await flow.set_attachment_enabled(
            "test-task:after-stats:extra-audit", "uf6", False, actor="admin"
        )
        self.assertEqual(detail["version"], 3)  # 停用也是新版本（不可变）
        self.assertNotIn("extra-audit", [n["id"] for n in detail["effective_nodes"]])
        dispose()
        self.assertEqual(flow.attachments(), [])

    # --------------------------------------------------------------- 删除规则
    async def test_delete_rules(self) -> None:
        flow = self.context.flow
        await flow.save_flow("uf7", {"extends": "test-task", "overrides": {}})
        await flow.delete_flow("uf7", actor="alice")
        with self.assertRaisesRegex(FlowError, "流程不存在"):
            await flow.delete_flow("uf7")
        with self.assertRaisesRegex(FlowError, "模板不可删除"):
            await flow.delete_flow("test-task")

    # ------------------------------------------------------- catalog 筛选（q）
    async def test_catalog_q_and_cluster_scope(self) -> None:
        self.register_defaults()
        flow = self.context.flow
        flow.step("dispatch", "dispatch-b", self.handler("dispatch-b"), labels={"impl": "b"})
        items = flow.catalog(q="dispatch")
        self.assertEqual(
            {item["id"] for item in items},
            {"dispatch/dispatch", "dispatch/dispatch-b"},
        )
        items = flow.catalog(labels={"impl": "b"})
        self.assertEqual({item["id"] for item in items}, {"dispatch/dispatch-b"})
        items = flow.catalog(scope="cluster")  # 无 nodes 服务：只含本机
        self.assertTrue(items)


class _FakeNodes:
    def select(self, *, kinds: Any = None, labels: Any = None, online_only: bool = True) -> list[dict[str, Any]]:
        return [
            {
                "node_id": "worker-1", "kind": "flow.step", "id": "execute/remote-echo",
                "provider": "test", "labels": {"stage": "execute"}, "summary": "远端步骤",
                "detail": {"idempotent": True, "max_concurrency": 2},
            }
        ]


class FlowUserApiTests(unittest.IsolatedAsyncioTestCase):
    """HTTP 面（附录 D 用户流程路由 + 权限点 + 页面注册）。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        config_path = root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\nname = test-node\n"
            f"\n[web]\nport = {self.port}\nauth_token = test-token\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(root / 'aetp.db').as_posix()}\n"
            "\n[flow]\n"
            "cancel_grace = 0.1\nretry_cap = 0.01\nstep_timeout = 5\n"
            "resolve_wait = 1\nresume_delay = 0\n",
            encoding="utf-8",
        )
        self.context = Context()
        self.context.provide("node_kind", "master")
        self.context.provide("node_name", "test-node")
        self.context.provide("nodes", _FakeNodes())
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)
        from common.plugins.webapi import WebApiPlugin

        await self.context.plugin(WebApiPlugin)
        await self.context.plugin(FlowService)
        self.context.flow.define(TEMPLATE)

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        for _ in range(50):
            try:
                self._temp.cleanup()
                break
            except PermissionError:
                await asyncio.sleep(0.05)

    async def api(
        self, path: str, *, method: str = "GET", payload: Any = None, token: str | None = AUTH_TOKEN
    ) -> tuple[int, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, raw = await arequest(
            self.base, path, method=method, body=body,
            content_type="application/json", token=token,
        )
        return status, json.loads(raw or b"null")

    async def test_flows_crud_routes(self) -> None:
        status, data = await self.api("/api/flow/flows")
        self.assertEqual(status, 200)
        self.assertEqual([f["id"] for f in data["flows"]], ["test-task"])

        status, data = await self.api(
            "/api/flow/flows/uf-api", method="PUT",
            payload={"extends": "test-task", "overrides": {"disable": ["report"]}},
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["created_by"], "operator")  # actor 取认证身份（附录 D）
        self.assertEqual(data["version"], 1)

        status, data = await self.api("/api/flow/flows/uf-api")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["versions"]), 1)
        self.assertEqual(data["export"]["$schema"], USER_FLOW_SCHEMA)

        status, data = await self.api(
            "/api/flow/flows/uf-api", method="PUT",
            payload={"extends": "test-task", "overrides": {"disable": ["split"]}},
        )
        self.assertEqual(status, 422)  # locked 拒绝
        self.assertEqual(data["code"], "preflight_failed")
        self.assertEqual(data["details"]["problems"][0]["code"], "locked")

        status, data = await self.api("/api/flow/flows/test-task", method="DELETE")
        self.assertEqual(status, 409)  # 模板不可删除
        status, _data = await self.api("/api/flow/flows/uf-api", method="DELETE")
        self.assertEqual(status, 200)
        status, _data = await self.api("/api/flow/flows/uf-api", method="DELETE")
        self.assertEqual(status, 404)

    async def test_catalog_filters_and_runs_window(self) -> None:
        self.context.flow.step("execute", "remote-echo", self._echo, labels={"stage": "execute"})
        self.context.flow.step("dispatch", "dispatch-a", self._echo)
        self.context.flow.step("dispatch", "dispatch-b", self._echo, labels={"impl": "b"})
        status, data = await self.api("/api/flow/catalog?labels.impl=b&q=dispatch")
        self.assertEqual(status, 200)
        self.assertEqual({s["id"] for s in data["steps"]}, {"dispatch/dispatch-b"})
        status, data = await self.api("/api/flow/catalog?q=dispatch")
        self.assertEqual(
            {s["id"] for s in data["steps"]}, {"dispatch/dispatch-a", "dispatch/dispatch-b"}
        )
        status, data = await self.api("/api/flow/catalog?scope=cluster&q=remote")
        self.assertEqual(status, 200)
        self.assertTrue(any(s["node_id"] == "worker-1" for s in data["steps"]))  # 合并从节点能力

        status, data = await self.api("/api/flow/runs?from=0&to=9999999999")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 0)
        status, data = await self.api("/api/flow/runs?from=oops")
        self.assertEqual(status, 400)

    async def _echo(self, _sc: StepContext) -> StepOutput:
        return {}

    async def test_attachments_routes(self) -> None:
        self.context.flow.attachment("test-task", "after-stats", "extra-audit", "audit")
        status, data = await self.api("/api/flow/attachments")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["attachments"]), 1)
        await self.api(
            "/api/flow/flows/uf-att", method="PUT",
            payload={"extends": "test-task", "overrides": {}},
        )
        status, data = await self.api(
            "/api/flow/attachments/test-task:after-stats:extra-audit", method="PUT",
            payload={"flow_id": "uf-att", "enabled": True},
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["version"], 2)
        status, data = await self.api("/api/flow/attachments?flow_id=uf-att")
        self.assertTrue(data["attachments"][0]["enabled"])

    async def test_ui_page_registers_via_inject(self) -> None:
        """flow_ui 注入式挂载：flow + webui 就位才注册「流程」页面（§5.14.8）。"""
        from common.plugins.flow.ui_service import FlowUiService

        self.assertEqual(FlowUiService.inject, ["flow", "webui"])
        stub = _WebUiStub()
        bare = Context()
        bare.provide("node_kind", "master")
        bare.provide("node_name", "t")
        bare.provide("appconfig", None)
        bare.provide("db", None)
        bare.provide("webui", stub)
        await bare.plugin(FlowService, {"resume_delay": 0})
        try:
            self.assertTrue(bare.flow is not None)
            ui = None
            for _attempt in range(40):  # 子 fiber 随依赖就绪异步激活（同 test_log）
                ui = bare.get("flow_ui")
                if ui is not None:
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(ui is not None)
            self.assertEqual(len(stub.calls), 1)
            contribution_id, options = stub.calls[0]
            self.assertEqual(contribution_id, "flow")
            self.assertEqual(options["entry"], "FlowPage.vue")
            self.assertEqual(options["path"], "/flow")
            self.assertEqual(options["title"], "流程")
        finally:
            await unload_all(bare)


if __name__ == "__main__":
    unittest.main()
