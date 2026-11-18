"""test_core 管理 API（api_service）与页面服务（ui_service）的端到端测试。

覆盖（对应任务清单 13 项）：spec_from_dict 解析（完整嵌套 / 缺省 / 未知字段 /
坏类型逐项 ValueError）、任务提交（成功、Idempotency-Key 头幂等、坏 JSON →
invalid_json、规格非法 → invalid_spec）、任务列表（state / labels 双格式过滤、
limit / offset 分页）、任务详情（task + stages + history 形态、404、
``test/collect-task`` extensions 收集槽且单次请求只派发一次）、Job 与聚合结果
查询（基本形态 / 404）、取消（含 reason、已终结 → 409 invalid_transition）、
重跑（failed 复活 attempt+1、非 failed → 409、缺失 404）、改优先级（生效与坏
body 400）、用例结果 / 产物过滤（经 ``ctx.comm.report(TOPIC_JOB_RESULT, ...)``
自环上报造数）、产物下载（local 文件回内容 / 外部 URI 302 / 缺失与坏路径 404）、
无令牌鉴权拒绝、/healthz 的 test_core 指标、UI 页面注册与卸载清理。

端到端栈：Context → appconfig + db + webapi + comm + TestCorePlugin（真实端口，
free_port 防撞车）；webui / nodes 用最小服务桩（真实契约分别由 test_web_ui /
heartbeat 测试覆盖）。用例 / 产物造数前先把 ``ctx.nodes`` 桩放开候选节点，
让派发循环把 Job 推进到 leased，结果上报才会聚合落库。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import tempfile
import time
import unittest
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlparse

from cordis_port import Context, Fiber

from _helpers import AUTH_TOKEN
from _helpers import arequest as _arequest
from _helpers import free_port as _free_port
from common.cordis_utils import unload_all, wait_until
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.comm import CommService
from common.plugins.db import DbPlugin
from common.plugins.webapi import WebApiPlugin
from common.test_contracts import (
    TEST_PROTO,
    TOPIC_JOB_RESULT,
    ExecutorSelector,
    ShardSpec,
    StageSpec,
    TaskSpec,
)
from master_plugins.test_core import TestCorePlugin as _TestCorePlugin
from master_plugins.test_core.api_service import spec_from_dict
from master_plugins.test_core.ui_service import TestCoreUiService as _TestCoreUiService


class _StubWebUi:
    """最小 webui 服务桩：只记录 register_ui（真实契约由 test_web_ui 覆盖）。"""

    def __init__(self) -> None:
        self.registrations: list[dict[str, object]] = []

    def register_ui(self, contribution_id: str, **options: object) -> None:
        self.registrations.append({"id": contribution_id, **options})


class _StubNodes:
    """调度器可选依赖 ``ctx.nodes`` 的桩：默认无候选（Job 停在 queued）。

    测试造数时放开 ``enabled``：返回「本节点即执行节点」的自环条目，派发消息
    走 comm 本地自环（无未送达重试噪音），Job 由派发循环推进到 leased。
    """

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        self.enabled = False

    def select(
        self, kinds: object = None, labels: object = None
    ) -> list[dict[str, object]]:
        if not self.enabled:
            return []
        # labels 是选择器标签的超集（scheduler 能力匹配口径，§9.2）
        return [
            {
                "kind": "executor",
                "id": "pytest",
                "node_id": self.node_id,
                "labels": {"lang": "python"},
            }
        ]

    def list(self, online_only: bool = False) -> list[dict[str, object]]:
        return self.select()


def _raw_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    token: str | None = AUTH_TOKEN,
) -> tuple[int, str | None, bytes]:
    """原样 HTTP 请求（http.client）：可带任意请求头、不跟随 302。

    ``_helpers.request`` 无法携带 Idempotency-Key 等自定义头，且 urllib 默认
    跟随 302（外部 URI 会炸未知协议），这两类断言走此封装，其余一律用 _helpers。
    """
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


def _task_body(
    name: str = "冒烟任务", *, labels: Mapping[str, str] | None = None, **extra: object
) -> dict[str, object]:
    """合法 TaskSpec 的 JSON 形态（字段名对齐 common.test_contracts dataclass）。"""
    body: dict[str, object] = {
        "name": name,
        "stages": [
            {
                "name": "unit",
                "executor": {"id": "pytest", "kind": "executor", "labels": {"lang": "python"}},
                "params": {"targets": ["tests/"]},
            }
        ],
        "labels": dict(labels or {}),
    }
    body.update(extra)
    return body


async def _boot_master(
    root: Path,
) -> tuple[Context, _StubWebUi, _StubNodes, str, Fiber]:
    """装配 test_core 全栈：appconfig + db + webapi + comm + TestCorePlugin。"""
    root.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    config_path = root / "config.ini"
    config_path.write_text(
        "[node]\nkind = master\nname = 测试主节点\n\n[web]\n"
        f"port = {port}\n"
        "auth_token = test-token\n"
        "\n[db]\n"
        f"url = sqlite+aiosqlite:///{(root / 'aetp.db').as_posix()}\n"
        "\n[comm]\n"
        "batch_interval = 0.05\n",
        encoding="utf-8",
    )
    context = Context()
    context.provide("node_kind", "master")
    await context.plugin(AppConfigPlugin, {"path": str(config_path)})
    context.provide(
        "node_name",
        context.appconfig.section("node").get("name") or socket.gethostname(),
    )
    await context.plugin(DbPlugin)
    await context.plugin(WebApiPlugin)
    await context.plugin(CommService)
    webui = _StubWebUi()
    context.provide("webui", webui)
    nodes = _StubNodes(str(context.get("node_name")))
    context.provide("nodes", nodes)
    fiber: Fiber = await context.plugin(_TestCorePlugin)
    # 等 TestCoreApiService 随依赖自动挂载（缺 webapi 的部署会安静 PENDING）
    ready = await wait_until(
        lambda: context.get("test_core_api") is not None, timeout=10.0, interval=0.05
    )
    if not ready:
        raise RuntimeError("TestCoreApiService 未在限时内挂载")
    return context, webui, nodes, f"http://127.0.0.1:{port}", fiber


# ============================================================== spec_from_dict


class SpecFromDictTests(unittest.TestCase):
    """spec_from_dict：完整嵌套解析、缺省值、未知字段忽略、坏类型逐项拒绝。"""

    def test_full_nested_parse(self) -> None:
        spec = spec_from_dict(
            {
                "name": "端到端",
                "stages": [
                    {
                        "name": "unit",
                        "executor": {
                            "id": "pytest",
                            "kind": "executor",
                            "labels": {"lang": "python"},
                        },
                        "params": {"targets": ["tests/"], "extra": "-q"},
                        "shard": {"mode": "plan", "hint": 3},
                        "requires": {"canoe": 1},
                        "timeout_s": 90,
                    },
                    {
                        "name": "report",
                        "executor": {"id": "report/html"},
                        "params": {},
                    },
                ],
                "priority": 5,
                "labels": {"project": "alpha"},
                "params": {"env": "ci"},
                "idempotency_key": "k-1",
                "failure_policy": "continue",
                "timeout_s": 600,
            }
        )
        self.assertEqual(
            spec,
            TaskSpec(
                name="端到端",
                stages=(
                    StageSpec(
                        name="unit",
                        executor=ExecutorSelector(
                            id="pytest", kind="executor", labels={"lang": "python"}
                        ),
                        params={"targets": ["tests/"], "extra": "-q"},
                        shard=ShardSpec(mode="plan", hint=3),
                        requires={"canoe": 1},
                        timeout_s=90.0,
                    ),
                    StageSpec(
                        name="report",
                        executor=ExecutorSelector(id="report/html"),
                        params={},
                    ),
                ),
                priority=5,
                labels={"project": "alpha"},
                params={"env": "ci"},
                idempotency_key="k-1",
                failure_policy="continue",
                timeout_s=600.0,
            ),
        )

    def test_defaults_when_optional_fields_absent(self) -> None:
        spec = spec_from_dict(
            {"name": "最小", "stages": [{"name": "s", "executor": {"id": "e"}, "params": {}}]}
        )
        self.assertEqual(spec.priority, 0)
        self.assertEqual(dict(spec.labels), {})
        self.assertEqual(dict(spec.params), {})
        self.assertEqual(spec.idempotency_key, "")
        self.assertEqual(spec.failure_policy, "fail_fast")
        self.assertEqual(spec.timeout_s, 0.0)
        stage = spec.stages[0]
        self.assertEqual(stage.executor.kind, "executor")
        self.assertEqual(dict(stage.executor.labels), {})
        self.assertEqual(stage.shard, ShardSpec(mode="none", hint=0))
        self.assertEqual(dict(stage.requires), {})
        self.assertEqual(stage.timeout_s, 0.0)

    def test_unknown_fields_are_ignored(self) -> None:
        spec = spec_from_dict(
            {
                "name": "带未知字段",
                "unknown_task": 1,
                "stages": [
                    {
                        "name": "s",
                        "executor": {"id": "e", "unknown_executor": 2},
                        "params": {},
                        "shard": {"mode": "none", "unknown_shard": 3},
                        "unknown_stage": 4,
                    }
                ],
            }
        )
        self.assertEqual(spec.name, "带未知字段")
        self.assertEqual(len(spec.stages), 1)

    def test_bad_types_raise_value_error(self) -> None:
        def stage(**extra: object) -> dict[str, object]:
            return {
                "name": "x",
                "stages": [
                    {
                        "name": "s",
                        "executor": {"id": "e"},
                        "params": {},
                        **extra,
                    }
                ],
            }

        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"name": 123, "stages": []}, "name 需是字符串"),
            ({"name": "x", "stages": "bad"}, "stages 需是数组"),
            (
                {
                    "name": "x",
                    "stages": [{"name": "s", "executor": "bad", "params": {}}],
                },
                "需是对象",
            ),
            ({"name": "x", "stages": [], "timeout_s": "bad"}, "timeout_s 需是非负有限数字"),
            ({"name": "x", "stages": [], "priority": True}, "priority 需是整数"),
            (stage(shard={"mode": "bad"}), "shard.mode 只能是"),
            ({"name": "x", "stages": [], "labels": {"a": 1}}, "labels.a 需是字符串"),
            ({"name": "x", "stages": [], "failure_policy": "maybe"}, "failure_policy 只能是"),
        )
        for data, pattern in cases:
            with self.subTest(data=data):
                with self.assertRaisesRegex(ValueError, pattern):
                    spec_from_dict(data)


# ============================================================== 端到端 API 栈


class _ApiStack:
    """端到端 API 栈装配 + 常用操作（各测试类混入，逐测例起独立栈）。"""

    context: Context
    webui: _StubWebUi
    nodes: _StubNodes
    base_url: str
    fiber: Fiber
    root: Path
    # 断言方法由混入宿主（IsolatedAsyncioTestCase）在运行时提供；
    # 此处纯注解声明类型面（不生成类属性），供混入方法内调用。
    assertEqual: Callable[..., None]
    assertIs: Callable[..., None]
    assertIsNotNone: Callable[..., None]

    async def asyncSetUp(self) -> None:
        self._temp: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        (
            self.context,
            self.webui,
            self.nodes,
            self.base_url,
            self.fiber,
        ) = await _boot_master(self.root)

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self._temp.cleanup()

    async def _call(
        self,
        path: str,
        *,
        method: str = "GET",
        body: object | None = None,
        token: str | None = AUTH_TOKEN,
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
            token=token,
        )
        return status, json.loads(payload) if payload else {}

    async def _submit(
        self, name: str = "冒烟任务", *, labels: Mapping[str, str] | None = None, **extra: object
    ) -> str:
        status, payload = await self._call(
            "/api/test/tasks", method="POST", body=_task_body(name, labels=labels, **extra)
        )
        self.assertEqual(status, 200)
        self.assertIs(payload["ok"], True)
        return str(payload["task_id"])

    async def _detail(self, task_id: str) -> dict[str, Any]:
        status, payload = await self._call(f"/api/test/tasks/{quote(task_id)}")
        self.assertEqual(status, 200)
        return payload

    async def _wait_value(
        self,
        fetch: Callable[[], Coroutine[Any, Any, object]],
        *,
        timeout: float = 15.0,
        interval: float = 0.05,
    ) -> object:
        """轮询（异步取数，不卡事件循环）直到 fetch() 真值或超时；返回末次取值。"""
        deadline = time.monotonic() + timeout
        value = await fetch()
        while not value and time.monotonic() < deadline:
            await asyncio.sleep(interval)
            value = await fetch()
        return value

    async def _wait_leased_job(self, task_id: str) -> dict[str, Any]:
        """等派发循环把任务的 Job 推进到 leased（结果上报聚合的前提）。"""

        async def fetch() -> object:
            status, payload = await self._call(f"/api/test/tasks/{quote(task_id)}/jobs")
            jobs = payload.get("jobs") if status == 200 else None
            candidates: list[dict[str, Any]] = jobs or []
            return next(
                (
                    job
                    for job in candidates
                    if job.get("state") in ("leased", "running")
                ),
                None,
            )

        job = await self._wait_value(fetch)
        self.assertIsNotNone(job, "job 未在限时内派发（leased）")
        return dict(cast(Mapping[str, Any], job))

    async def _report_result(
        self,
        job: Mapping[str, Any],
        *,
        status: str = "succeeded",
        summary: Mapping[str, Any] | None = None,
        cases: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        """经 comm 自环上报一条 JobResult（制造 case / artifact 数据）。"""
        payload = {
            "proto": TEST_PROTO,
            "job_id": job["job_id"],
            "attempt": int(job["attempt"]),
            "status": status,
            "summary": dict(summary or {"total": 1, "passed": 1}),
            "cases": list(cases or []),
            "artifacts": list(artifacts or []),
            "started_ts": time.time() - 1.0,
            "finished_ts": time.time(),
            "error": "",
        }
        self.context.comm.report(TOPIC_JOB_RESULT, payload)

    async def _make_failed_task(self, name: str = "失败任务") -> tuple[str, dict[str, Any]]:
        """造一个 failed 任务：派发 → 上报失败结果 → 等屏障收尾（fail_fast）。"""
        self.nodes.enabled = True
        task_id = await self._submit(name)
        job = await self._wait_leased_job(task_id)
        await self._report_result(
            job,
            status="failed",
            summary={"total": 1, "failed": 1},
            cases=[
                {
                    "case_id": "c-fail",
                    "status": "failed",
                    "duration_s": 0.1,
                    "message": "boom",
                    "detail_ref": "",
                }
            ],
        )

        async def fetch() -> object:
            payload = await self._detail(task_id)
            return payload if payload["task"]["state"] == "failed" else None

        done = await self._wait_value(fetch)
        self.assertIsNotNone(done, "任务未在限时内收敛到 failed")
        return task_id, job


class TaskSubmitApiTests(_ApiStack, unittest.IsolatedAsyncioTestCase):
    """POST /api/test/tasks：成功、幂等键、错误映射。"""

    async def test_submit_success_and_idempotency_key(self) -> None:
        body = _task_body("首次提交", labels={"project": "alpha"}, priority=3)
        status, payload = await self._call("/api/test/tasks", method="POST", body=body)
        self.assertEqual(status, 200)
        self.assertIs(payload["ok"], True)
        first_id = str(payload["task_id"])
        self.assertTrue(first_id)

        # Idempotency-Key 头（_helpers.request 带不了自定义头，走原样请求）
        def submit_with_key() -> tuple[int, str | None, bytes]:
            return _raw_request(
                self.base_url,
                "/api/test/tasks",
                method="POST",
                body=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "Idempotency-Key": "ci-42"},
            )

        status, _location, raw = await asyncio.to_thread(submit_with_key)
        self.assertEqual(status, 200)
        keyed_id = str(json.loads(raw)["task_id"])
        status, _location, raw = await asyncio.to_thread(submit_with_key)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["task_id"], keyed_id)  # 重复提交同 id

        # 幂等命中不新增任务：库里只留首提 + 一条幂等任务
        status, payload = await self._call("/api/test/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(
            {str(item["task_id"]) for item in payload["tasks"]}, {first_id, keyed_id}
        )

    async def test_submit_error_mapping(self) -> None:
        # 坏 JSON → 400 invalid_json
        status, payload = await self._call_raw_json(b"{oops")
        self.assertEqual(status, 400)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["code"], "invalid_json")

        # 规格类型非法 → 400 invalid_spec（error 消息含问题）
        status, payload = await self._call_raw_json(
            json.dumps({"name": 123, "stages": []}).encode("utf-8")
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_spec")
        self.assertIn("name", str(payload["error"]))

        status, payload = await self._call_raw_json(
            json.dumps({"name": "x", "stages": "bad"}).encode("utf-8")
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_spec")
        self.assertIn("stages", str(payload["error"]))

        # 规格语义非法（空 stages，落库前 ensure_valid 拒绝）→ 400 invalid_spec
        status, payload = await self._call_raw_json(
            json.dumps({"name": "x", "stages": []}).encode("utf-8")
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_spec")
        self.assertIn("stages", str(payload["error"]))

    async def _call_raw_json(self, body: bytes) -> tuple[int, dict[str, Any]]:
        status, payload = await _arequest(
            self.base_url,
            "/api/test/tasks",
            method="POST",
            body=body,
            content_type="application/json",
        )
        return status, cast(dict[str, Any], json.loads(payload))


class TaskListApiTests(_ApiStack, unittest.IsolatedAsyncioTestCase):
    """GET /api/test/tasks：state / labels（JSON 与 k=v 双格式）过滤与分页。"""

    async def test_state_and_labels_filters(self) -> None:
        task_a = await self._submit("A", labels={"project": "alpha", "env": "ci"})
        task_b = await self._submit("B", labels={"project": "beta"})
        task_c = await self._submit("C", labels={"project": "alpha"})

        status, payload = await self._call("/api/test/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 3)

        # labels 过滤：JSON 格式（子集匹配）
        query = quote(json.dumps({"project": "alpha"}))
        status, payload = await self._call(f"/api/test/tasks?labels={query}")
        self.assertEqual(
            {str(item["task_id"]) for item in payload["tasks"]}, {task_a, task_c}
        )

        # labels 过滤：k=v 逗号分隔
        status, payload = await self._call("/api/test/tasks?labels=project=alpha,env=ci")
        self.assertEqual({str(item["task_id"]) for item in payload["tasks"]}, {task_a})

        # state 过滤：先把 B 推进终态
        status, payload = await self._call(
            f"/api/test/tasks/{quote(task_b)}/cancel", method="POST", body={}
        )
        self.assertEqual(status, 200)
        status, payload = await self._call("/api/test/tasks?state=canceled")
        self.assertEqual({str(item["task_id"]) for item in payload["tasks"]}, {task_b})
        for item in payload["tasks"]:
            self.assertEqual(item["state"], "canceled")
        status, payload = await self._call("/api/test/tasks?state=running")
        self.assertEqual(
            {str(item["task_id"]) for item in payload["tasks"]}, {task_a, task_c}
        )

    async def test_limit_offset_pagination(self) -> None:
        ids = {await self._submit(name) for name in ("A", "B", "C")}
        status, page1 = await self._call("/api/test/tasks?limit=2&offset=0")
        self.assertEqual(status, 200)
        self.assertEqual(len(page1["tasks"]), 2)
        self.assertEqual(page1["limit"], 2)
        self.assertEqual(page1["offset"], 0)
        self.assertEqual(page1["count"], 2)

        status, page2 = await self._call("/api/test/tasks?limit=2&offset=2")
        self.assertEqual(len(page2["tasks"]), 1)

        # 并集全覆盖、无重复（不作顺序断言：created_ts 同刻时顺序不保证）
        first = {str(item["task_id"]) for item in page1["tasks"]}
        second = {str(item["task_id"]) for item in page2["tasks"]}
        self.assertFalse(first & second)
        self.assertEqual(first | second, ids)

        status, page3 = await self._call("/api/test/tasks?limit=2&offset=10")
        self.assertEqual(page3["tasks"], [])


class TaskDetailApiTests(_ApiStack, unittest.IsolatedAsyncioTestCase):
    """GET /api/test/tasks/{id}：task + stages + history、404、extensions 收集槽。"""

    async def test_detail_includes_task_stages_history(self) -> None:
        task_id = await self._submit("详情任务", labels={"env": "ci"})
        payload = await self._detail(task_id)
        self.assertIs(payload["ok"], True)

        task = cast(dict[str, Any], payload["task"])
        self.assertEqual(task["task_id"], task_id)
        self.assertEqual(task["name"], "详情任务")
        self.assertEqual(task["labels"], {"env": "ci"})

        stages = cast(list[dict[str, Any]], payload["stages"])
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]["name"], "unit")
        self.assertEqual(stages[0]["executor"]["id"], "pytest")

        history = cast(list[dict[str, Any]], payload["history"])
        self.assertTrue(history)  # submit 已产生状态迁移审计
        for entry in history:
            self.assertIn("from_state", entry)
            self.assertIn("to_state", entry)
            self.assertIn("reason", entry)
            self.assertIn("ts", entry)
        self.assertTrue(any(entry["to_state"] == "running" for entry in history))

        self.assertIn("extensions", payload)
        self.assertIn("extension_errors", payload)

    async def test_detail_missing_returns_404(self) -> None:
        status, payload = await self._call("/api/test/tasks/no-such-task")
        self.assertEqual(status, 404)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["code"], "not_found")

    async def test_detail_extensions_slot_dispatched_once(self) -> None:
        calls: list[dict[str, object]] = []

        def contribute(response: dict[str, object]) -> None:
            calls.append(response)
            cast(dict[str, object], response["extensions"])["demo"] = {"ok": True}

        self.context.on("test/collect-task", contribute)
        task_id = await self._submit("扩展任务")
        payload = await self._detail(task_id)
        self.assertEqual(payload["extensions"]["demo"], {"ok": True})
        self.assertEqual(payload["extension_errors"], [])
        # 一次详情请求只派发一次（防核心 / API 两层重复收集）
        self.assertEqual(len(calls), 1)


class TaskJobsResultApiTests(_ApiStack, unittest.IsolatedAsyncioTestCase):
    """GET …/jobs、…/result：基本形态与 404。"""

    async def test_jobs_and_result_shape(self) -> None:
        task_id = await self._submit("查询任务")
        status, payload = await self._call(f"/api/test/tasks/{quote(task_id)}/jobs")
        self.assertEqual(status, 200)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["task_id"], task_id)
        self.assertEqual(payload["count"], 1)
        job = cast(dict[str, Any], payload["jobs"][0])
        self.assertEqual(job["task_id"], task_id)
        self.assertEqual(job["attempt"], 1)
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["results"], [])  # attempt 历史槽

        status, payload = await self._call(f"/api/test/tasks/{quote(task_id)}/result")
        self.assertEqual(status, 200)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["task_id"], task_id)
        result = cast(dict[str, Any], payload["result"])
        self.assertEqual(result["task_id"], task_id)
        for key in ("state", "counts", "jobs", "cases", "artifacts", "duration_s"):
            self.assertIn(key, result)

    async def test_jobs_and_result_missing_returns_404(self) -> None:
        for path in (
            "/api/test/tasks/no-such/jobs",
            "/api/test/tasks/no-such/result",
        ):
            with self.subTest(path=path):
                status, payload = await self._call(path)
                self.assertEqual(status, 404)
                self.assertEqual(payload["code"], "not_found")


class TaskCancelApiTests(_ApiStack, unittest.IsolatedAsyncioTestCase):
    """POST …/cancel：成功（含 reason）、已终结 409、不存在 404。"""

    async def test_cancel_with_reason_then_terminal_409(self) -> None:
        task_id = await self._submit("取消任务")
        status, payload = await self._call(
            f"/api/test/tasks/{quote(task_id)}/cancel",
            method="POST",
            body={"reason": "不需要了"},
        )
        self.assertEqual(status, 200)
        self.assertIs(payload["ok"], True)
        self.assertIs(payload["canceled"], True)

        detail = await self._detail(task_id)
        self.assertIn(detail["task"]["state"], ("canceling", "canceled"))
        reasons = [entry["reason"] for entry in detail["history"]]
        self.assertIn("不需要了", reasons)  # reason 进状态迁移审计

        # 已终结任务不可再取消 → 409 invalid_transition
        status, payload = await self._call(
            f"/api/test/tasks/{quote(task_id)}/cancel", method="POST", body={}
        )
        self.assertEqual(status, 409)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["code"], "invalid_transition")

    async def test_cancel_missing_returns_404(self) -> None:
        status, payload = await self._call(
            "/api/test/tasks/no-such/cancel", method="POST", body={}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "not_found")


class TaskRerunApiTests(_ApiStack, unittest.IsolatedAsyncioTestCase):
    """POST …/rerun：failed 复活（attempt+1）、非 failed 409、不存在 404。"""

    async def test_rerun_failed_task_requeues_job(self) -> None:
        task_id, job = await self._make_failed_task("重跑任务")
        status, payload = await self._call(
            f"/api/test/tasks/{quote(task_id)}/rerun", method="POST", body={}
        )
        self.assertEqual(status, 200)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["task_id"], task_id)

        # 同一 Job 行 attempt+1；首轮结果留档
        status, jobs = await self._call(f"/api/test/tasks/{quote(task_id)}/jobs")
        entry = cast(dict[str, Any], jobs["jobs"][0])
        self.assertEqual(entry["job_id"], job["job_id"])
        self.assertEqual(entry["attempt"], 2)
        self.assertEqual(len(entry["results"]), 1)

        detail = await self._detail(task_id)
        self.assertEqual(detail["task"]["state"], "running")

    async def test_rerun_rejects_non_failed_and_missing(self) -> None:
        task_id = await self._submit("非失败任务")
        status, payload = await self._call(
            f"/api/test/tasks/{quote(task_id)}/rerun", method="POST", body={}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "invalid_transition")

        status, payload = await self._call(
            "/api/test/tasks/no-such/rerun", method="POST", body={}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "not_found")


class TaskPatchApiTests(_ApiStack, unittest.IsolatedAsyncioTestCase):
    """PATCH …：改优先级生效、坏 body 400、不存在 404。"""

    async def test_patch_priority_and_bad_body(self) -> None:
        task_id = await self._submit("改优先级")
        path = f"/api/test/tasks/{quote(task_id)}"
        status, payload = await self._call(path, method="PATCH", body={"priority": 7})
        self.assertEqual(status, 200)
        self.assertIs(payload["ok"], True)
        self.assertIs(payload["updated"], True)
        self.assertEqual(payload["priority"], 7)

        detail = await self._detail(task_id)
        self.assertEqual(detail["task"]["priority"], 7)

        for body in ({"nope": 1}, {"priority": True}, {"priority": "high"}):
            with self.subTest(body=body):
                status, payload = await self._call(path, method="PATCH", body=body)
                self.assertEqual(status, 400)
                self.assertIs(payload["ok"], False)

        # 坏 JSON body → 400
        status, payload = await _arequest(
            self.base_url,
            path,
            method="PATCH",
            body=b"not-json",
            content_type="application/json",
        )
        self.assertEqual(status, 400)

        status, payload = await self._call(
            "/api/test/tasks/no-such", method="PATCH", body={"priority": 1}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "not_found")


class CaseArtifactQueryApiTests(_ApiStack, unittest.IsolatedAsyncioTestCase):
    """GET /api/test/cases、/api/test/artifacts：结果上报造数后过滤参数生效。"""

    async def test_case_and_artifact_filters(self) -> None:
        self.nodes.enabled = True
        task_id = await self._submit("用例任务", labels={"project": "alpha"})
        job = await self._wait_leased_job(task_id)
        await self._report_result(
            job,
            summary={"total": 3, "passed": 1, "failed": 1, "skipped": 1},
            cases=[
                {"case_id": "c-pass", "status": "passed", "duration_s": 0.1, "message": "", "detail_ref": ""},
                {"case_id": "c-fail", "status": "failed", "duration_s": 0.2, "message": "boom", "detail_ref": "a-1"},
                {"case_id": "c-skip", "status": "skipped", "duration_s": 0.0, "message": "", "detail_ref": ""},
            ],
            artifacts=[
                {"artifact_id": "a-1", "kind": "report", "name": "report.html", "uri": "local://tmp/report.html", "size": 10, "sha256": "f" * 64},
                {"artifact_id": "a-2", "kind": "log", "name": "run.log", "uri": "local://tmp/run.log", "size": 20, "sha256": "e" * 64},
            ],
        )

        async def cases_ready() -> object:
            _status, payload = await self._call(f"/api/test/cases?task_id={quote(task_id)}")
            return payload if payload.get("count") == 3 else None

        ready = await self._wait_value(cases_ready)
        self.assertIsNotNone(ready, "结果上报后用例未落库")

        # status 过滤
        _status, payload = await self._call(
            f"/api/test/cases?task_id={quote(task_id)}&status=failed"
        )
        self.assertEqual(
            [str(case["case_id"]) for case in payload["cases"]], ["c-fail"]
        )

        # job_id 过滤
        _status, payload = await self._call(f"/api/test/cases?job_id={quote(str(job['job_id']))}")
        self.assertEqual(payload["count"], 3)

        # limit 截断
        _status, payload = await self._call(
            f"/api/test/cases?task_id={quote(task_id)}&limit=2"
        )
        self.assertEqual(payload["count"], 2)

        # 无命中
        _status, payload = await self._call("/api/test/cases?task_id=no-such")
        self.assertEqual(payload["cases"], [])

        # 产物：task_id / state 过滤
        _status, payload = await self._call(f"/api/test/artifacts?task_id={quote(task_id)}")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            {str(item["artifact_id"]) for item in payload["artifacts"]}, {"a-1", "a-2"}
        )
        _status, payload = await self._call(
            f"/api/test/artifacts?task_id={quote(task_id)}&state=local"
        )
        self.assertEqual(payload["count"], 2)
        _status, payload = await self._call(
            f"/api/test/artifacts?task_id={quote(task_id)}&state=uploaded"
        )
        self.assertEqual(payload["artifacts"], [])
        _status, payload = await self._call("/api/test/artifacts?task_id=no-such")
        self.assertEqual(payload["artifacts"], [])


class ArtifactDownloadApiTests(_ApiStack, unittest.IsolatedAsyncioTestCase):
    """GET /api/test/artifacts/{id}/download：local 回文件 / 外部 URI 302 / 404。"""

    async def test_download_local_redirect_and_missing(self) -> None:
        file_path = self.root / "artifact.txt"
        file_path.write_text("产物内容", encoding="utf-8")
        missing_path = self.root / "nope.log"

        self.nodes.enabled = True
        task_id = await self._submit("产物任务")
        job = await self._wait_leased_job(task_id)
        await self._report_result(
            job,
            artifacts=[
                {
                    "artifact_id": "a-file",
                    "kind": "report",
                    "name": "artifact.txt",
                    "uri": f"local://{file_path.as_posix()}",
                    "size": file_path.stat().st_size,
                    "sha256": "f" * 64,
                },
                {
                    "artifact_id": "a-oss",
                    "kind": "report",
                    "name": "oss.html",
                    "uri": "oss://bucket/oss.html",
                    "size": 1,
                    "sha256": "e" * 64,
                },
                {
                    "artifact_id": "a-gone",
                    "kind": "log",
                    "name": "gone.log",
                    "uri": f"local://{missing_path.as_posix()}",
                    "size": 0,
                    "sha256": "d" * 64,
                },
            ],
        )

        async def artifacts_ready() -> object:
            _status, payload = await self._call(
                f"/api/test/artifacts?task_id={quote(task_id)}"
            )
            return payload if payload.get("count") == 3 else None

        ready = await self._wait_value(artifacts_ready)
        self.assertIsNotNone(ready, "产物未登记")

        # local:// 指向真实文件 → 200 回内容
        status, body = await _arequest(
            self.base_url, "/api/test/artifacts/a-file/download"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.decode("utf-8"), "产物内容")

        # 外部 URI → 302 Location（不跟随重定向）
        status, location, _body = await asyncio.to_thread(
            _raw_request, self.base_url, "/api/test/artifacts/a-oss/download"
        )
        self.assertEqual(status, 302)
        self.assertEqual(location, "oss://bucket/oss.html")

        # 不存在的产物 → 404
        status, payload = await self._call("/api/test/artifacts/no-such/download")
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "not_found")

        # local:// 指向不存在路径 → 404
        status, payload = await self._call("/api/test/artifacts/a-gone/download")
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "not_found")


class AuthApiTests(_ApiStack, unittest.IsolatedAsyncioTestCase):
    """鉴权：不带 / 带错令牌的请求被 webapi auth 链拒绝（401）。"""

    async def test_missing_or_wrong_token_rejected(self) -> None:
        for token in (None, "wrong-token"):
            with self.subTest(token=token):
                status, payload = await self._call("/api/test/tasks", token=token)
                self.assertEqual(status, 401)
                self.assertIn("X-AETP-Token", str(payload.get("error", "")))


class HealthzApiTests(_ApiStack, unittest.IsolatedAsyncioTestCase):
    """/healthz：汇总 test_core 健康指标。"""

    async def test_healthz_reports_test_core_metrics(self) -> None:
        status, payload = await self._call("/healthz")
        self.assertEqual(status, 200)
        metrics = cast(dict[str, Any], payload["metrics"]["test_core"])
        for key in (
            "queue_depth",
            "inflight_jobs",
            "running_tasks",
            "lease_timeouts_total",
            "retries_total",
        ):
            self.assertIn(key, metrics)


class UiServiceTests(_ApiStack, unittest.IsolatedAsyncioTestCase):
    """UI 注册：TestCoreUiService 页面贡献存在；卸载后清理干净。"""

    async def test_page_registered_and_unload_cleanup(self) -> None:
        self.assertEqual(_TestCoreUiService.inject, ["test_core", "webui"])
        self.assertIsInstance(self.context.get("test_core_ui"), _TestCoreUiService)

        items = [item for item in self.webui.registrations if item["id"] == "tasks"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["path"], "/tasks")
        self.assertEqual(item["title"], "测试任务")
        self.assertEqual(item["entry"], "TasksPage.vue")
        self.assertTrue(
            (Path(str(item["resource_root"])) / str(item["entry"])).is_file()
        )

        # 卸载清理：附属服务注销、管理路由随 fiber dispose 撤除
        outcome = self.fiber.dispose()
        if outcome is not None:
            await outcome
        self.assertIsNone(self.context.get("test_core_ui"))
        self.assertIsNone(self.context.get("test_core_api"))
        status, _payload = await self._call("/api/test/tasks")
        self.assertEqual(status, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
