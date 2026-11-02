"""flow HTTP API（Phase 3 / 附录 D 子集）测试：目录 / 校验 / 启动 202 / 预检 422 /
取消 / 恢复 / 权限（§7.5「HTTP：目录 / 校验 / 启动 202 / 预检 422 / 取消 /
恢复 / 权限」必测）。

真实栈：appconfig + db + webapi + flow（FlowApiService 随 init 自动挂载，
inject flow + webapi——「界面即服务」约定）。
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
from common.flow_contracts import FlowDef, JsonValue, NodeDef, StepContext, StepOutput
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.db import DbPlugin
from common.plugins.flow import FlowService
from common.plugins.webapi import WebApiPlugin


def _flow(flow_id: str, *stages: str) -> FlowDef:
    return FlowDef(
        id=flow_id,
        version=1,
        nodes=tuple(
            NodeDef(f"n{index}", stage) for index, stage in enumerate(stages, 1)
        ),
    )


class FlowApiTests(unittest.IsolatedAsyncioTestCase):
    """HTTP 接口行为（每个用例独立装配最小栈 + 真实端口）。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.calls: list[str] = []
        config_path = root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\nname = test-node\n"
            f"\n[web]\nport = {self.port}\nauth_token = test-token\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(root / 'aetp.db').as_posix()}\n"
            "\n[flow]\n"
            "cancel_grace = 0.1\nretry_cap = 0.01\nstep_timeout = 5\n"
            "resolve_wait = 0.5\n",
            encoding="utf-8",
        )
        self.context = Context()
        self.context.provide("node_kind", "master")
        self.context.provide("node_name", "test-node")
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)
        await self.context.plugin(WebApiPlugin)
        await self.context.plugin(FlowService)  # 自动挂 FlowApiService（webapi 就位）

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        for _ in range(50):  # Windows：aiosqlite 后台线程关文件略有延迟
            try:
                self._temp.cleanup()
                break
            except PermissionError:
                await asyncio.sleep(0.05)

    # ------------------------------------------------------------- 辅助
    def handler(self, name: str, output: dict[str, JsonValue] | None = None):
        async def run(_sc: StepContext) -> StepOutput:
            self.calls.append(name)
            return output if output is not None else {"ok": name}

        return run

    async def api(
        self, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None,
        token: str | None = AUTH_TOKEN,
    ) -> tuple[int, dict[str, Any]]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, raw = await arequest(
            self.base, path, method=method, body=body,
            content_type="application/json", token=token,
        )
        return status, json.loads(raw or b"{}")

    async def start_run(
        self, flow_id: str, payload: dict[str, Any] | None = None
    ) -> str:
        status, data = await self.api(
            f"/api/flow/flows/{flow_id}/runs", method="POST", payload=payload or {}
        )
        self.assertEqual(status, 202, data)
        return str(data["run_id"])

    async def wait_run(
        self, run_id: str, states: tuple[str, ...] = ("succeeded", "failed", "cancelled")
    ) -> dict[str, Any]:
        for _ in range(100):
            status, data = await self.api(f"/api/flow/runs/{run_id}")
            self.assertEqual(status, 200, data)
            if data["state"] in states:
                return data
            await asyncio.sleep(0.05)
        self.fail(f"run {run_id} 未到终态：{data['state']}")

    # --------------------------------------------------------- 目录 / 校验
    async def test_catalog_and_validate(self) -> None:
        """目录返回能力信封；validate 返回问题清单（不产生 run）。"""
        flow = self.context.flow
        flow.define(_flow("c1", "alpha"))
        flow.step("alpha", "a", self.handler("a"), labels={"format": "html"})
        status, data = await self.api("/api/flow/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["steps"]), 1)
        self.assertEqual(data["steps"][0]["kind"], "flow.step")

        status, data = await self.api("/api/flow/catalog?stage=alpha")
        self.assertEqual(len(data["steps"]), 1)
        status, data = await self.api("/api/flow/catalog?stage=other")
        self.assertEqual(data["steps"], [])

        status, data = await self.api(
            "/api/flow/flows/c1/validate", method="POST", payload={}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["problems"], [])
        status, data = await self.api("/api/flow/flows/ghost/validate", method="POST")
        self.assertEqual(status, 404)
        self.assertEqual(data["code"], "not_found")

    # ------------------------------------------------------------- 启动 202
    async def test_start_202_and_detail(self) -> None:
        """启动 202 + run_id；详情含 run + 节点时间线（§7.2 错误信封兼容）。"""
        flow = self.context.flow
        flow.define(_flow("c2", "alpha", "beta"))
        flow.step("alpha", "a", self.handler("a"))
        flow.step("beta", "b", self.handler("b"))
        run_id = await self.start_run("c2", {"input": {"x": 1}, "actor": "tester"})
        data = await self.wait_run(run_id)
        self.assertEqual(data["state"], "succeeded")
        self.assertEqual(data["initiator"], "operator")  # actor 取认证身份，body 自报被忽略（附录 D）
        self.assertEqual(
            [node["node_id"] for node in data["nodes"]], ["n1", "n2"]
        )
        self.assertTrue(all(node["state"] == "succeeded" for node in data["nodes"]))
        self.assertEqual(self.calls, ["a", "b"])

    # --------------------------------------------------------- 预检 422 信封
    async def test_preflight_rejects_with_422(self) -> None:
        """预检未过 → 422 + {error, code, details.problems}；不产生 run。"""
        flow = self.context.flow
        flow.define(_flow("c3", "missing-stage"))
        status, data = await self.api(
            "/api/flow/flows/c3/runs", method="POST", payload={}
        )
        self.assertEqual(status, 422)
        self.assertEqual(data["code"], "preflight_failed")
        self.assertTrue(data["details"]["problems"])
        self.assertEqual(data["details"]["problems"][0]["code"], "no_provider")
        status, listing = await self.api("/api/flow/runs")
        self.assertEqual(listing["total"], 0)  # 拒绝不产生半个 run（§5.9）

    # ------------------------------------------------------------- 取消 / 冲突
    async def test_cancel_and_conflict(self) -> None:
        """取消：在途 run 收到信号 → cancelled；终态再取消 → 409。"""
        gate = asyncio.Event()

        async def slow(_sc: StepContext) -> StepOutput:
            self.calls.append("slow")
            await gate.wait()
            return {"ok": True}

        flow = self.context.flow
        flow.define(_flow("c4", "alpha"))
        flow.step("alpha", "slow", slow)
        run_id = await self.start_run("c4")
        for _ in range(100):  # 等尝试真正起跑（handler 已挂 gate）再取消
            if self.calls:
                break
            await asyncio.sleep(0.02)
        status, data = await self.api(
            f"/api/flow/runs/{run_id}/cancel", method="POST",
            payload={"reason": "test", "actor": "tester"},
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        data = await self.wait_run(run_id, states=("cancelled", "failed"))
        self.assertEqual(data["state"], "cancelled")
        status, data = await self.api(
            f"/api/flow/runs/{run_id}/cancel", method="POST", payload={}
        )
        self.assertEqual(status, 409)
        self.assertEqual(data["code"], "conflict")

    # ------------------------------------------------------------- 列表 / 过滤
    async def test_list_runs_filtering(self) -> None:
        """列表：flow_id 过滤 + 分页 total（新 → 旧）。"""
        flow = self.context.flow
        flow.define(_flow("c5a", "alpha"))
        flow.define(_flow("c5b", "alpha"))
        flow.step("alpha", "a", self.handler("a"))
        await self.wait_run(await self.start_run("c5a"))
        await self.wait_run(await self.start_run("c5b"))
        _status, data = await self.api("/api/flow/runs")
        self.assertEqual(data["total"], 2)
        _status, data = await self.api("/api/flow/runs?flow_id=c5a")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["runs"][0]["flow_id"], "c5a")
        _status, data = await self.api("/api/flow/runs?state=succeeded&limit=1")
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["runs"]), 1)

    # --------------------------------------------------------- 恢复 / 404 / 权限
    async def test_resume_auth_and_404(self) -> None:
        """resume 仅限 interrupted（否则 409）；未知 run 404；无凭证被拒。"""
        status, data = await self.api("/api/flow/runs/ghost")
        self.assertEqual(status, 404)
        self.assertEqual(data["code"], "not_found")

        flow = self.context.flow
        flow.define(_flow("c6", "alpha"))
        flow.step("alpha", "a", self.handler("a"))
        run_id = await self.start_run("c6")
        await self.wait_run(run_id)
        status, data = await self.api(
            f"/api/flow/runs/{run_id}/resume", method="POST", payload={"mode": "retry"}
        )
        self.assertEqual(status, 409)  # succeeded 不可处置
        self.assertEqual(data["code"], "conflict")

        status, _data = await self.api("/api/flow/catalog", token=None)
        self.assertIn(status, (401, 403))  # 无凭证被鉴权拒绝

    # ------------------------------------------------------------- 健康探针
    async def test_healthz_reports_flow(self) -> None:
        """healthz 聚合 flow 指标（runs_started 等计数，§5.13）。"""
        flow = self.context.flow
        flow.define(_flow("c7", "alpha"))
        flow.step("alpha", "a", self.handler("a"))
        await self.wait_run(await self.start_run("c7"))
        metrics: dict[str, Any] = {}
        for _ in range(50):  # 计数在 run 收尾任务里落账，稍等生效
            status, data = await self.api("/healthz")
            self.assertEqual(status, 200)
            metrics = data["metrics"]["flow"]
            if metrics.get("runs_succeeded", 0) >= 1:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(metrics["runs_started"], 1.0)
        self.assertEqual(metrics["runs_succeeded"], 1.0)


if __name__ == "__main__":
    unittest.main()
