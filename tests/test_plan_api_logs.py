"""W1-B：日志 name_prefix 查询 + 计划 / 日志 / 材料过滤 API（设计稿 §4.4 / §4.5 / §6）。

1. ``LogStore.query`` / ``LogPlugin.query_logs`` 的 ``name_prefix`` 前缀过滤
   （精确命中、与 level / q AND 组合、LIKE 转义安全）；
2. ``GET /api/test/jobs/{job_id}/logs`` 游标端点（name_prefix 隔离、旧→新、
   since_id 游标、limit 截断、next_since_id、log 缺席降级）；
3. ``/api/test/plans`` CRUD + clone + dispatch（PlanNotFound→404 plan_not_found、
   InvalidSpec→400、「副本」命名、Idempotency-Key 头优先）；
4. ``/api/test/materials`` executor 过滤（X-Material-Executor 头 + ?executor=
   谓词 ``executor_id == X OR = ''``，通用材料可见）。

栈引导照 tests/test_test_core_api.py（webapi HTTP 栈 + _helpers + AUTH_TOKEN）。
"""

from __future__ import annotations

import asyncio
import http.client
import io
import json
import tempfile
import time
import unittest
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlparse

from cordis_port import Context

from _helpers import AUTH_TOKEN
from _helpers import arequest as _arequest
from _helpers import free_port as _free_port
from common.cordis_utils import unload_all, wait_until
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.comm import CommService
from common.plugins.db import DbPlugin
from common.plugins.log import LogPlugin, LogStore
from common.plugins.log.store import LogEntry
from common.plugins.webapi import WebApiPlugin
from common.test_contracts import TEST_PROTO, TOPIC_JOB_LOG
from master_plugins.test_core import TestCorePlugin as _TestCorePlugin
from master_plugins.test_core.models import TestJob as _TestJob

#: 最小可用材料内容（zip 内容对核心不透明）
_MIN_ZIP: dict[str, str] = {"cases/test_a.py": "def test_a():\n    assert True\n"}


# ================================================================ 纯函数辅助
def _line(msg: str, *, level: int = 20, ts: float = 100.0) -> dict[str, Any]:
    """test.job.log 行（{ts, level, msg}）。"""
    return {"ts": ts, "level": level, "msg": msg}


def _log_entry(name: str, msg: str, *, level: int = 20, op: str = "") -> dict[str, Any]:
    """入库条目形状（对齐 log 插件出口：ts/name/level/op/msg/source）。"""
    return {"ts": time.time(), "name": name, "level": level, "op": op, "msg": msg, "source": ""}


def _zip_bytes(files: Mapping[str, str | bytes]) -> bytes:
    """把 ``{zip 内相对路径: 内容}`` 打成 zip 字节（材料上传用）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in files.items():
            package.writestr(name, content)
    return buffer.getvalue()


def _raw_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    token: str | None = AUTH_TOKEN,
) -> tuple[int, str | None, bytes]:
    """原样 HTTP 请求（http.client）：可带 Idempotency-Key / X-Material-Executor 等自定义头。"""
    parsed = urlparse(base_url)
    connection = http.client.HTTPConnection(
        cast(str, parsed.hostname), parsed.port, timeout=10
    )
    merged = dict(headers or {})
    if token:
        merged["X-AETP-Token"] = token
    try:
        connection.request(method, path, body=body, headers=merged)
        response = connection.getresponse()
        payload = response.read()
        return response.status, response.getheader("Location"), payload
    finally:
        connection.close()


class _StubWebUi:
    """最小 webui 服务桩：只记录 register_ui（真实契约由 test_web_ui 覆盖）。"""

    def __init__(self) -> None:
        self.registrations: list[dict[str, object]] = []

    def register_ui(self, contribution_id: str, **options: object) -> None:
        self.registrations.append({"id": contribution_id, **options})


# ================================================================ name_prefix
class LogNamePrefixStoreTests(unittest.IsolatedAsyncioTestCase):
    """LogStore.query 的 name_prefix 前缀过滤（转义规则同 q，AND 组合）。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        config_path = root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(root / 'aetp.db').as_posix()}\n",
            encoding="utf-8",
        )
        self.context = Context()
        self.context.provide("node_kind", "master")
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)
        db = self.context.get("db")
        await db.sync_schema(models=[LogEntry])
        self.store = LogStore(db)

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self._temp.cleanup()

    async def _write(self, *names: str) -> None:
        await self.store.write_batch(
            "n1", [_log_entry(name, f"msg-{name}") for name in names]
        )

    async def test_prefix_filters_names_precisely(self) -> None:
        await self._write("test.j1.1", "test.j1.2", "test.j2.1", "test.j1x.1", "other.thing")
        self.assertEqual(
            {row["name"] for row in await self.store.query(name_prefix="test.j1.")},
            {"test.j1.1", "test.j1.2"},  # 尾点隔离：test.j1x.1 不命中
        )
        self.assertEqual(
            [row["name"] for row in await self.store.query(name_prefix="test.j2.")],
            ["test.j2.1"],
        )
        self.assertEqual(len(await self.store.query(name_prefix="test.")), 4)
        self.assertEqual(
            {row["name"] for row in await self.store.query(name_prefix="test.j1")},
            {"test.j1.1", "test.j1.2", "test.j1x.1"},
        )
        self.assertEqual(await self.store.query(name_prefix="no-such."), [])
        self.assertEqual(len(await self.store.query()), 5)  # 缺省不过滤
        self.assertEqual(len(await self.store.query(name_prefix="")), 5)  # 空串不过滤

    async def test_prefix_combines_with_level_and_q(self) -> None:
        await self.store.write_batch(
            "n1",
            [
                _log_entry("test.j1.a", "alpha only", level=30),
                _log_entry("test.j1.b", "shared needle", level=10),
                _log_entry("test.j2.c", "shared needle", level=20),
            ],
        )
        self.assertEqual(
            {row["name"] for row in await self.store.query(name_prefix="test.j1.", level=20)},
            {"test.j1.b"},  # AND：前缀 ∩ level ≤ 20
        )
        self.assertEqual(
            {row["name"] for row in await self.store.query(name_prefix="test.j1.", q="shared")},
            {"test.j1.b"},  # AND：前缀 ∩ 关键字
        )
        self.assertEqual(
            {row["name"] for row in await self.store.query(name_prefix="test.j2.", q="shared")},
            {"test.j2.c"},
        )
        self.assertEqual(
            {row["name"] for row in await self.store.query(q="shared")},
            {"test.j1.b", "test.j2.c"},
        )
        self.assertEqual(
            {row["name"] for row in await self.store.query(level=20)},
            {"test.j1.b", "test.j2.c"},  # level 是阈值（≤），与前缀无关时不受限
        )

    async def test_prefix_escapes_like_wildcards(self) -> None:
        """前缀里的 % / _ / \\ 按字面匹配（转义先例同 q），不作通配符。"""
        await self._write(
            "test.p%1.1",
            "test.px1.1",
            "test.p_1.1",
            "test.pX1.1",
            "test.p\\1.1",
        )
        self.assertEqual(
            {row["name"] for row in await self.store.query(name_prefix="test.p%1.")},
            {"test.p%1.1"},  # % 不做单字符通配
        )
        self.assertEqual(
            {row["name"] for row in await self.store.query(name_prefix="test.p_1.")},
            {"test.p_1.1"},  # _ 不做单字符通配
        )
        self.assertEqual(
            {row["name"] for row in await self.store.query(name_prefix="test.p\\1.")},
            {"test.p\\1.1"},  # 反斜杠按字面
        )


class LogNamePrefixPluginTests(unittest.IsolatedAsyncioTestCase):
    """LogPlugin.query_logs 的 name_prefix 透传（read-your-writes 落库后过滤）。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._temp.name)
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'aetp.db').as_posix()}\n"
            "\n[log]\n"
            "console = false\n"
            "file =\n"
            "store = true\n"
            "report = false\n",
            encoding="utf-8",
        )
        self.context = Context()
        self.context.provide("node_kind", "master")
        self.context.provide("node_name", "log-node")
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)
        await self.context.plugin(LogPlugin)

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self._temp.cleanup()

    async def test_query_logs_forwards_name_prefix(self) -> None:
        log = self.context.get("log")
        log.ingest_remote(
            {
                "logs": [
                    _log_entry("test.j1.1", "第一行", op="op-1"),
                    _log_entry("test.j1.2", "第二行", op="op-1"),
                    _log_entry("test.j2.1", "他组", op="op-2"),
                ]
            },
            {"node": "exec-1", "topic": "log.batch", "ts": time.time(), "received_ts": time.time(), "id": ""},
        )
        rows = await log.query_logs(name_prefix="test.j1.")  # read-your-writes
        self.assertEqual({row["name"] for row in rows}, {"test.j1.1", "test.j1.2"})
        self.assertEqual({row["op"] for row in rows}, {"op-1"})
        self.assertEqual(len(await log.query_logs()), 3)  # 缺省不过滤
        self.assertEqual(await log.query_logs(name_prefix="test.j3."), [])


# ================================================================ 端到端 API 栈
class _MasterStack:
    """全栈基类：appconfig + db + webapi + comm + [log] + test_core（逐测例起栈）。"""

    context: Context | None
    base_url: str
    root: Path
    # 断言方法由混入宿主（IsolatedAsyncioTestCase）运行期提供；纯注解声明类型面。
    assertEqual: Callable[..., None]

    async def asyncSetUp(self) -> None:
        self._temp: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.context = None
        self.base_url = ""

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
            self.context = None
        self._temp.cleanup()

    async def boot(self, *, with_log: bool = True) -> None:
        port = _free_port()
        config_path = self.root / "config.ini"
        log_ini = "[log]\nconsole = false\nfile =\nstore = true\nreport = false\n" if with_log else ""
        config_path.write_text(
            "[node]\nkind = master\nname = w1b-node\n\n[web]\n"
            f"port = {port}\n"
            "auth_token = test-token\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'aetp.db').as_posix()}\n"
            "\n[comm]\n"
            "batch_interval = 0.05\n"
            "\n" + log_ini,
            encoding="utf-8",
        )
        context = Context()
        self.context = context
        context.provide("node_kind", "master")
        context.provide("node_name", "w1b-node")
        await context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await context.plugin(DbPlugin)
        await context.plugin(WebApiPlugin)
        await context.plugin(CommService)
        if with_log:
            await context.plugin(LogPlugin)
        context.provide("webui", _StubWebUi())
        await context.plugin(_TestCorePlugin)
        ready = await wait_until(
            lambda: context.get("test_core_api") is not None, timeout=10.0, interval=0.05
        )
        if not ready:
            raise RuntimeError("TestCoreApiService 未在限时内挂载")
        self.base_url = f"http://127.0.0.1:{port}"

    async def _call(
        self, path: str, *, method: str = "GET", body: object | None = None
    ) -> tuple[int, dict[str, Any]]:
        """JSON 请求 / 响应封装（_helpers.arequest），返回 (状态码, 载荷)。"""
        raw = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None
        )
        status, payload = await _arequest(
            self.base_url,
            path,
            method=method,
            body=raw,
            content_type="application/json" if raw is not None else None,
        )
        return status, cast("dict[str, Any]", json.loads(payload)) if payload else {}

    async def seed_job(self, *, job_id: str, task_id: str) -> None:
        """直接播种一行 TestJob（日志 op=task_id 追踪断言用，免调度派发）。"""
        assert self.context is not None
        core = self.context.get("test_core")
        await core._repo(_TestJob).add(
            _TestJob(
                id=job_id,
                stage_id="s-1",
                task_id=task_id,
                node_id="executor-1",
                attempt=1,
                state="running",
                shard="",
                lease_ts=0.0,
                started_ts=0.0,
                finished_ts=0.0,
                error="",
                result_seq=0,
            )
        )

    async def send_job_log(
        self,
        *,
        job_id: str,
        attempt: int = 1,
        seq: int = 0,
        case_id: str = "",
        stream: str = "stdout",
        lines: Sequence[Mapping[str, Any]],
    ) -> None:
        """直调核心 _handle_job_log 造数（meta 用远端节点，入汇聚点日志库）。"""
        assert self.context is not None
        core = self.context.get("test_core")
        await core._handle_job_log(
            {
                "proto": TEST_PROTO,
                "job_id": job_id,
                "attempt": attempt,
                "seq": seq,
                "stream": stream,
                "case_id": case_id,
                "lines": [dict(item) for item in lines],
            },
            {
                "node": "executor-1",
                "topic": TOPIC_JOB_LOG,
                "ts": time.time(),
                "received_ts": time.time(),
                "id": "",
            },
        )


class JobLogsApiTests(_MasterStack, unittest.IsolatedAsyncioTestCase):
    """GET /api/test/jobs/{job_id}/logs：前缀隔离 / 旧→新 / 游标 / 截断 / 降级。"""

    async def test_filters_by_job_prefix_old_to_new(self) -> None:
        await self.boot()
        await self.seed_job(job_id="j1", task_id="t-9")
        await self.send_job_log(job_id="j1", attempt=1, seq=0, lines=[_line("a1"), _line("a2")])
        await self.send_job_log(job_id="j1", attempt=2, seq=0, lines=[_line("b1")])
        await self.send_job_log(job_id="j2", attempt=1, seq=0, lines=[_line("c1")])
        await self.send_job_log(job_id="j1x", attempt=1, seq=0, lines=[_line("d1")])
        status, body = await self._call("/api/test/jobs/j1/logs")
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)
        self.assertIs(body["log_available"], True)
        logs = cast("list[dict[str, Any]]", body["logs"])
        self.assertEqual([row["msg"] for row in logs], ["a1", "a2", "b1"])  # 旧→新
        self.assertEqual({row["name"] for row in logs}, {"test.j1.1", "test.j1.2"})
        self.assertEqual({row["op"] for row in logs}, {"t-9"})  # op = task_id 原样透传
        self.assertEqual({row["node"] for row in logs}, {"executor-1"})
        for row in logs:
            self.assertEqual(set(row), {"id", "ts", "node", "name", "level", "op", "source", "msg"})
        self.assertEqual(body["next_since_id"], max(int(row["id"]) for row in logs))

    async def test_since_id_cursor_and_limit_clamp(self) -> None:
        await self.boot()
        for index in range(3):
            await self.send_job_log(
                job_id="jc", attempt=1, seq=index, lines=[_line(f"行{index}")]
            )
        status, body = await self._call("/api/test/jobs/jc/logs")
        rows = cast("list[dict[str, Any]]", body["logs"])
        ids = [int(row["id"]) for row in rows]
        self.assertEqual([row["msg"] for row in rows], ["行0", "行1", "行2"])
        self.assertEqual(ids, sorted(ids))
        # 游标：since_id = 第二条 id → 只回第三条
        status, body = await self._call(f"/api/test/jobs/jc/logs?since_id={ids[1]}")
        rows = cast("list[dict[str, Any]]", body["logs"])
        self.assertEqual([row["msg"] for row in rows], ["行2"])
        self.assertEqual(body["next_since_id"], ids[2])
        # 空批保持游标不回退
        status, body = await self._call(f"/api/test/jobs/jc/logs?since_id={ids[2]}")
        self.assertEqual(body["logs"], [])
        self.assertEqual(body["next_since_id"], ids[2])
        # limit 截断：无游标 = 尾部页（store 首屏取最新 N），返回旧→新
        status, body = await self._call("/api/test/jobs/jc/logs?limit=2")
        rows = cast("list[dict[str, Any]]", body["logs"])
        self.assertEqual([row["msg"] for row in rows], ["行1", "行2"])
        self.assertEqual(body["next_since_id"], ids[2])
        # 带游标截断：after_id 序取最旧 N
        status, body = await self._call(f"/api/test/jobs/jc/logs?since_id={ids[0]}&limit=1")
        rows = cast("list[dict[str, Any]]", body["logs"])
        self.assertEqual([row["msg"] for row in rows], ["行1"])
        self.assertEqual(body["next_since_id"], ids[1])
        # limit 钳到 [1, 1000]：0 → 1
        status, body = await self._call("/api/test/jobs/jc/logs?limit=0")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["logs"]), 1)

    async def test_log_absent_degrades(self) -> None:
        await self.boot(with_log=False)
        status, body = await self._call("/api/test/jobs/j1/logs")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "logs": [], "log_available": False})

    async def test_bad_limit_maps_400(self) -> None:
        await self.boot()
        status, body = await self._call("/api/test/jobs/j1/logs?limit=abc")
        self.assertEqual(status, 400)
        self.assertIs(body["ok"], False)


# ================================================================ 计划 API
class PlansApiTests(_MasterStack, unittest.IsolatedAsyncioTestCase):
    """/api/test/plans：CRUD / 错误映射 / clone 命名 / dispatch 幂等（§4.4 / §6）。"""

    async def _material_id(self) -> str:
        """上传（幂等复用）计划步骤绑定的材料 id（v2 保存必填 material）。"""
        assert self.context is not None
        core = self.context.get("test_core")
        result = await core.upload_material(name="脚本包", data=_zip_bytes(_MIN_ZIP))
        return str(result["id"])

    def _plan_payload(
        self, name: str = "回归计划", *, material_id: str = "", **extra: object
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "name": name,
            "description": "说明",
            "labels": {"team": "qa"},
            "defaults": {"priority": 5, "failure_policy": "fail_fast", "timeout_s": 0},
            "steps": [
                {
                    "name": "单测",
                    "executor": {"kind": "executor", "id": "pytest", "labels": {}},
                    "material": {"material_id": material_id},
                    "params": {"targets": ["tests/"]},
                }
            ],
        }
        body.update(extra)
        return body

    async def _create(self, name: str = "回归计划") -> dict[str, Any]:
        payload = self._plan_payload(name, material_id=await self._material_id())
        status, body = await self._call("/api/test/plans", method="POST", body=payload)
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)
        return cast("dict[str, Any]", body["plan"])

    async def test_plan_crud_lifecycle(self) -> None:
        await self.boot()
        plan = await self._create()
        plan_id = str(plan["id"])
        self.assertTrue(plan_id)
        self.assertEqual(plan["name"], "回归计划")
        # 列表（keyword / 计数形态固定 §6）
        status, body = await self._call("/api/test/plans")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"ok", "plans", "count"})
        self.assertEqual(body["count"], 1)
        status, body = await self._call(f"/api/test/plans?keyword={quote('回归')}")
        self.assertEqual(body["count"], 1)
        status, body = await self._call(f"/api/test/plans?keyword={quote('不存在的词')}")
        self.assertEqual(body["count"], 0)
        # 详情
        status, body = await self._call(f"/api/test/plans/{quote(plan_id)}")
        self.assertEqual(status, 200)
        self.assertEqual(body["plan"]["name"], "回归计划")
        # 更新
        status, body = await self._call(
            f"/api/test/plans/{quote(plan_id)}",
            method="PUT",
            body=self._plan_payload(
                description="新说明", material_id=await self._material_id()
            ),
        )
        self.assertEqual(status, 200)
        status, body = await self._call(f"/api/test/plans/{quote(plan_id)}")
        self.assertEqual(body["plan"]["description"], "新说明")
        # 删除后详情 404
        status, body = await self._call(f"/api/test/plans/{quote(plan_id)}", method="DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "deleted": True})
        status, body = await self._call(f"/api/test/plans/{quote(plan_id)}")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "plan_not_found")
        status, body = await self._call("/api/test/plans")
        self.assertEqual(body["count"], 0)

    async def test_missing_plan_maps_404(self) -> None:
        await self.boot()
        cases: tuple[tuple[str, str, dict[str, object] | None], ...] = (
            ("GET", f"/api/test/plans/{quote('p-nope')}", None),
            ("PUT", f"/api/test/plans/{quote('p-nope')}", self._plan_payload()),
            ("DELETE", f"/api/test/plans/{quote('p-nope')}", None),
            ("POST", f"/api/test/plans/{quote('p-nope')}/clone", {}),
            ("POST", f"/api/test/plans/{quote('p-nope')}/dispatch", {}),
        )
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                status, payload = await self._call(path, method=method, body=body)
                self.assertEqual(status, 404)
                self.assertIs(payload["ok"], False)
                self.assertEqual(payload["code"], "plan_not_found")

    async def test_bad_body_maps_400(self) -> None:
        await self.boot()
        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"description": "没有 name"}, "name"),
            ({"name": 123, "stages": []}, "name"),
            ({"name": "x", "steps": "bad"}, "steps"),
            ({"name": "x", "steps": [1]}, "steps"),
            ({"name": "x", "labels": {"a": 1}}, "labels"),
            ({"name": "x", "trigger": 5}, "trigger"),  # trigger 需是对象或缺省（§5）
            ({"name": "x", "notify": "bad"}, "notify"),
        )
        for body, pattern in cases:
            with self.subTest(body=body, pattern=pattern):
                status, payload = await self._call("/api/test/plans", method="POST", body=body)
                self.assertEqual(status, 400)
                self.assertIs(payload["ok"], False)
                self.assertEqual(payload["code"], "invalid_spec")
                self.assertIn(pattern, str(payload["error"]))
        # 语义校验（核心 save_plan 逐条报）：步骤 name 重复 → InvalidSpec → 400
        duplicate = self._plan_payload(
            steps=[
                {"name": "a", "executor": {"id": "pytest"}, "params": {}},
                {"name": "a", "executor": {"id": "pytest"}, "params": {}},
            ]
        )
        status, payload = await self._call("/api/test/plans", method="POST", body=duplicate)
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_spec")

    async def test_clone_adds_suffix_and_created_by(self) -> None:
        await self.boot()
        plan = await self._create()
        plan_id = str(plan["id"])
        # body 可选 created_by
        status, body = await self._call(
            f"/api/test/plans/{quote(plan_id)}/clone", method="POST", body={"created_by": "alice"}
        )
        self.assertEqual(status, 200)
        clone = cast("dict[str, Any]", body["plan"])
        self.assertEqual(clone["name"], "回归计划 副本")
        self.assertNotEqual(str(clone["id"]), plan_id)
        self.assertEqual(clone["created_by"], "alice")
        status, body = await self._call(f"/api/test/plans/{quote(plan_id)}")
        source = cast("dict[str, Any]", body["plan"])
        self.assertEqual(clone["steps"], source["steps"])
        self.assertEqual(clone["labels"], source["labels"])
        # query 可选 created_by（body 缺席）
        status, body = await self._call(
            f"/api/test/plans/{quote(plan_id)}/clone?created_by={quote('bob')}", method="POST", body={}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["plan"]["created_by"], "bob")

    async def test_dispatch_idempotency_header_wins(self) -> None:
        await self.boot()
        plan = await self._create()
        path = f"/api/test/plans/{quote(str(plan['id']))}/dispatch"

        def dispatch(key_header: str | None, body_key: str) -> dict[str, Any]:
            body = json.dumps({"idempotency_key": body_key}, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if key_header is not None:
                headers["Idempotency-Key"] = key_header
            status, _location, payload = _raw_request(
                self.base_url, path, method="POST", body=body, headers=headers
            )
            self.assertEqual(status, 200)
            return cast("dict[str, Any]", json.loads(payload))

        first = await asyncio.to_thread(dispatch, "k1", "b1")
        self.assertIs(first["ok"], True)
        self.assertTrue(str(first["task_id"]))
        self.assertIs(first["deduplicated"], False)
        # 同头（k1）+ 不同 body 键 → 命中头键幂等
        second = await asyncio.to_thread(dispatch, "k1", "b2")
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertIs(second["deduplicated"], True)
        # 头换键（k2）+ 命中过的 body 键 → body 键不参与幂等，新建任务
        third = await asyncio.to_thread(dispatch, "k2", "b1")
        self.assertNotEqual(third["task_id"], first["task_id"])
        self.assertIs(third["deduplicated"], False)

    async def test_plan_trigger_notify_roundtrip_and_enum(self) -> None:
        """trigger / notify 经 API 落库回显；枚举归核心校验（编辑器 v2 设计 §5）。"""
        await self.boot()
        mid = await self._material_id()
        payload = self._plan_payload(
            "触发计划",
            material_id=mid,
            trigger={"type": "cron", "config": {"expr": "0 0 * * *"}},
            notify={"type": "webhook", "config": {}},
        )
        status, body = await self._call("/api/test/plans", method="POST", body=payload)
        self.assertEqual(status, 200)
        plan = cast("dict[str, Any]", body["plan"])
        self.assertEqual(plan["trigger"], {"type": "cron", "config": {"expr": "0 0 * * *"}})
        self.assertEqual(plan["notify"], {"type": "webhook", "config": {}})
        bad = self._plan_payload("坏触发", material_id=mid, trigger={"type": "bogus"})
        status, body = await self._call("/api/test/plans", method="POST", body=bad)
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_spec")
        self.assertIn("trigger.type", str(body["error"]))

    async def test_dispatch_overrides_errors_map_400(self) -> None:
        await self.boot()
        plan = await self._create()
        path = f"/api/test/plans/{quote(str(plan['id']))}/dispatch"
        cases: tuple[dict[str, object], ...] = (
            {"bogus": 1},  # overrides 未知键 → InvalidSpec（§5.5）
            {"env": {"AETP_X": "1"}},  # AETP_* 键 → InvalidSpec（评审 #9）
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                status, payload = await self._call(path, method="POST", body=overrides)
                self.assertEqual(status, 400)
                self.assertIs(payload["ok"], False)
                self.assertEqual(payload["code"], "invalid_spec")


# ================================================================ 材料 executor 过滤
class MaterialsExecutorFilterApiTests(_MasterStack, unittest.IsolatedAsyncioTestCase):
    """GET /api/test/materials?executor= 与 X-Material-Executor 头（§4.5 / §6）。"""

    async def _upload(self, *, name: str, executor: str | None = None) -> dict[str, Any]:
        # 名字走 query（HTTP 头仅 latin-1），执行器归属走 X-Material-Executor 头
        headers = {"Content-Type": "application/zip"}
        if executor is not None:
            headers["X-Material-Executor"] = executor
        status, _location, payload = await asyncio.to_thread(
            _raw_request,
            self.base_url,
            f"/api/test/materials?name={quote(name)}",
            method="POST",
            body=_zip_bytes({"cases/a.py": name}),  # 内容随名变化，避开指纹去重
            headers=headers,
        )
        self.assertEqual(status, 200)
        return cast("dict[str, Any]", json.loads(payload))

    async def test_executor_filter_and_universal_visible(self) -> None:
        await self.boot()
        await self._upload(name="pytest 包", executor="pytest")
        await self._upload(name="canoe 包", executor="canoe")
        await self._upload(name="通用包")  # 无 X-Material-Executor = 通用

        async def names(query: str) -> set[str]:
            status, body = await self._call(f"/api/test/materials{query}")
            self.assertEqual(status, 200)
            return {str(item["name"]) for item in cast("list[dict[str, Any]]", body["materials"])}

        self.assertEqual(await names("?executor=pytest"), {"pytest 包", "通用包"})  # ==X OR =''
        self.assertEqual(await names("?executor=canoe"), {"canoe 包", "通用包"})
        self.assertEqual(await names("?executor=nosuch"), {"通用包"})
        self.assertEqual(await names(""), {"pytest 包", "canoe 包", "通用包"})  # 空 = 不过滤


if __name__ == "__main__":
    unittest.main()
