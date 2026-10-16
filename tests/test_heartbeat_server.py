"""主模块心跳插件（master_plugins/heartbeat）的单元 / 集成测试。

覆盖设计稿 §9 测试计划：协议校验、状态机推进、管理 API、与从模块
（slave_plugins/heartbeat）对打的全链路、卸载清理。
"""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from typing import cast
from urllib.parse import quote

from cordis_port import Context, Fiber
from starlette.requests import Request
from starlette.types import Message, Scope

from _helpers import arequest as _arequest
from _helpers import free_port as _free_port
from _helpers import request as _request
from common.cordis_utils import unload_all, wait_until
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.webapi import WebApiPlugin
from master_plugins.heartbeat import (
    NODE_STATUS_EVENT,
    HeartbeatServerConfig,
    HeartbeatServerError,
    HeartbeatServerPlugin,
    NodeRegistry,
    NodeStatus,
)
from master_plugins.heartbeat.plugin import MAX_BODY_BYTES, read_json_body
from slave_plugins.heartbeat import HeartbeatPlugin
from slave_plugins.heartbeat.capabilities import capabilities_digest

#: 主模块侧阈值（插件 / 集成测试用小值，避免真实等待）
_FAST_SECTION = (
    "\n[heartbeat]\n"
    "interval = 0.3\n"
    "timeout = 1.0\n"
    "max_backoff = 2.0\n"
    "offline_timeout = 0.8\n"
    "lost_timeout = 2.0\n"
    "sweep_interval = 0.2\n"
)


class _Clock:
    """可控时间源（状态机测试免真实等待）。"""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _StubWebUi:
    """最小 webui 服务桩：只记录 register_ui（真实契约由 test_web_ui 覆盖）。"""

    def __init__(self) -> None:
        self.registrations: list[dict[str, object]] = []

    def register_ui(self, contribution_id: str, **options: object) -> None:
        self.registrations.append({"id": contribution_id, **options})


async def _boot_master(
    root: Path, *, section: str = ""
) -> tuple[Context, _StubWebUi, str, Fiber]:
    """装配带心跳主模块的节点：appconfig + webapi + heartbeat-server。"""
    root.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    config_path = root / "config.ini"
    config_path.write_text(
        "[node]\nkind = master\nname = 主节点\n\n[web]\n"
        f"port = {port}\n{section}",
        encoding="utf-8",
    )
    context = Context()
    context.provide("node_kind", "master")
    await context.plugin(AppConfigPlugin, {"path": str(config_path)})
    context.provide(
        "node_name",
        context.appconfig.section("node").get("name") or socket.gethostname(),
    )
    await context.plugin(WebApiPlugin)
    webui = _StubWebUi()
    context.provide("webui", webui)
    fiber: Fiber = await context.plugin(HeartbeatServerPlugin)
    return context, webui, f"http://127.0.0.1:{port}", fiber


def _beat_payload(**overrides: object) -> dict[str, object]:
    """一次合法心跳载荷（协议 §2），可按需覆盖字段。"""
    payload: dict[str, object] = {
        "node_id": "从节点-1",
        "node_kind": "slave",
        "address": "http://127.0.0.1:8081",
        "seq": 1,
        "sent_at": 1760000000.0,
        "status": "alive",
        "caps_digest": "digest-1",
    }
    payload.update(overrides)
    return payload


def _fake_request(body: bytes) -> Request:
    """合成一个只带请求体的 starlette Request（请求体上限直测用）。"""

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = cast(
        Scope,
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/heartbeat",
            "raw_path": b"/api/heartbeat",
            "query_string": b"",
            "headers": [
                (b"content-length", str(len(body)).encode()),
                (b"host", b"testserver"),
            ],
        },
    )
    return Request(scope, receive)


class BodyLimitTests(unittest.IsolatedAsyncioTestCase):
    """请求体上限（413）与非对象 JSON：合成请求直测，结果确定。"""

    async def test_rejects_oversized_body(self) -> None:
        with self.assertRaises(HeartbeatServerError) as caught:
            await read_json_body(_fake_request(b"x" * (MAX_BODY_BYTES + 1)))
        self.assertEqual(caught.exception.status_code, 413)

    async def test_rejects_non_object_json(self) -> None:
        with self.assertRaises(HeartbeatServerError):
            await read_json_body(_fake_request(b"[1]"))
        with self.assertRaises(HeartbeatServerError):
            await read_json_body(_fake_request(b"not json"))

    async def test_rejects_deeply_nested_json(self) -> None:
        """深嵌套 JSON 抛 RecursionError（非 ValueError 子类）：按不合规处理而非 500。"""
        with self.assertRaises(HeartbeatServerError):
            await read_json_body(_fake_request(b"[" * 60_000 + b"]" * 60_000))


class HeartbeatServerConfigTests(unittest.TestCase):
    """[heartbeat] 配置节解析与校验。"""

    def test_from_ini_defaults_and_values(self) -> None:
        config = HeartbeatServerConfig.from_ini({"offline_timeout": "20"})
        self.assertEqual(config.interval, 5.0)
        self.assertEqual(config.offline_timeout, 20.0)
        self.assertEqual(config.history_size, 200)
        self.assertEqual(HeartbeatServerConfig.to_ini({"interval": 3}), {"interval": "3"})

    def test_rejects_bad_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "未知配置项"):
            HeartbeatServerConfig.from_ini({"nope": "1"})
        with self.assertRaisesRegex(ValueError, "interval"):
            HeartbeatServerConfig.from_ini({"interval": "fast"})
        with self.assertRaisesRegex(ValueError, "offline_timeout"):
            HeartbeatServerConfig.from_ini({"offline_timeout": "0"})
        with self.assertRaisesRegex(ValueError, "history_size"):
            HeartbeatServerConfig.from_ini({"history_size": "0"})
        with self.assertRaisesRegex(ValueError, "lost_timeout"):
            HeartbeatServerConfig.from_ini({"offline_timeout": "50", "lost_timeout": "10"})


class NodeRegistryTests(unittest.TestCase):
    """注册表：心跳 upsert、状态机、能力存档与查询。"""

    def setUp(self) -> None:
        self.clock = _Clock()
        self.events: list[tuple[str, dict[str, object]]] = []
        self.logs: list[str] = []
        self.registry = NodeRegistry(
            HeartbeatServerConfig(
                interval=5.0, offline_timeout=15.0, lost_timeout=50.0, sweep_interval=5.0
            ),
            emit=lambda name, payload: self.events.append((name, payload)),
            log=self.logs.append,
            clock=self.clock,
        )

    def test_accept_upserts_and_tracks_seq(self) -> None:
        record, need_caps = self.registry.accept(_beat_payload(seq=5))
        self.assertTrue(need_caps)  # 首拍无存档 → 请求全量能力
        self.assertEqual(record.status, NodeStatus.ONLINE)
        self.assertEqual(record.first_seen, record.last_seen)

        # 重复 / 乱序心跳：刷新 last_seen，但不推进 seq
        self.clock.advance(1)
        record, _ = self.registry.accept(_beat_payload(seq=3))
        self.assertEqual(record.seq, 5)
        self.assertEqual(record.last_seen, self.clock.now)

        # seq 归 1 = 从模块重启：保留 first_seen，记录事件
        first_seen = record.first_seen
        record, _ = self.registry.accept(_beat_payload(seq=1))
        self.assertEqual(record.seq, 1)
        self.assertEqual(record.first_seen, first_seen)
        self.assertIn("restart", [entry["kind"] for entry in self.registry.history()])

        # 换地址记录事件
        self.registry.accept(_beat_payload(seq=2, address="http://127.0.0.1:9000"))
        self.assertIn("address", [entry["kind"] for entry in self.registry.history()])

    def test_state_machine_and_events(self) -> None:
        record, _ = self.registry.accept(_beat_payload())
        self.registry.sweep()
        self.assertEqual(record.status, NodeStatus.ONLINE)

        self.clock.advance(16)  # 超过 offline_timeout
        self.registry.sweep()
        self.assertEqual(record.status, NodeStatus.OFFLINE)

        self.clock.advance(40)  # 超过 lost_timeout
        self.registry.sweep()
        self.assertEqual(record.status, NodeStatus.LOST)
        self.assertEqual(
            [name for name, _ in self.events],
            [NODE_STATUS_EVENT, NODE_STATUS_EVENT],
        )
        self.assertEqual(record.missed, 11)  # 进入 lost 那一刻：56 秒 / interval 5

        # 失联后 missed 冻结，不再累计
        self.clock.advance(100)
        self.registry.sweep()
        self.assertEqual(record.status, NodeStatus.LOST)
        self.assertEqual(record.missed, 11)

        # 心跳回来即恢复并重新计数
        record, _ = self.registry.accept(_beat_payload())
        self.assertEqual(record.status, NodeStatus.ONLINE)
        self.assertEqual(record.missed, 0)

    def test_capability_archive_dedup_and_query(self) -> None:
        entries: list[dict[str, object]] = [
            {"kind": "executor", "id": "pytest", "provider": "runner", "summary": "单测"},
            {"kind": "executor", "id": "pytest", "provider": "other"},  # 重复：后到覆盖
            {"bad": "entry"},  # 不合信封：入 unparsed，不静默丢弃
        ]
        record, need_caps = self.registry.accept(
            _beat_payload(caps_digest=capabilities_digest(entries), capabilities=entries)
        )
        self.assertFalse(need_caps)  # 摘要与存档一致 → 不再索要
        self.assertEqual(len(record.caps), 1)
        self.assertEqual(record.caps[0].provider, "other")
        self.assertEqual(len(record.unparsed), 1)
        self.assertTrue(any("重复" in message for message in self.logs))

        found = self.registry.capabilities(kinds=["executor"], q="pytest")
        self.assertEqual([entry["node_id"] for entry in found], ["从节点-1"])
        self.assertEqual(self.registry.capabilities(kinds=["nope"]), [])
        self.assertEqual(self.registry.capabilities(labels={"lang": "python"}), [])

        # 摘要不一致（无全量载荷）→ 请求全量
        _, need_caps = self.registry.accept(_beat_payload(caps_digest="changed"))
        self.assertTrue(need_caps)

    def test_remove_purges_history(self) -> None:
        self.registry.accept(_beat_payload())
        self.clock.advance(16)
        self.registry.sweep()  # online → offline，写入历史
        self.assertTrue(self.registry.history("从节点-1"))

        self.assertTrue(self.registry.remove("从节点-1"))
        self.assertFalse(self.registry.remove("从节点-1"))
        self.assertIsNone(self.registry.get("从节点-1"))
        self.assertEqual(self.registry.history("从节点-1"), [])

    def test_rejects_invalid_payload(self) -> None:
        for payload in (
            {"node_kind": "slave"},  # 缺 node_id
            {"node_id": "  "},  # 空白
            {"node_id": "a/b"},  # 路径字符
            _beat_payload(seq="x"),  # seq 非整数
            _beat_payload(seq=-1),
        ):
            with self.assertRaises(HeartbeatServerError):
                self.registry.accept(payload)


class HeartbeatServerPluginTests(unittest.IsolatedAsyncioTestCase):
    """HTTP 协议与管理 API（真实 web 服务）。"""

    async def asyncSetUp(self) -> None:
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.context, self.webui, self.base_url, self.fiber = await _boot_master(
            Path(self.temp_dir.name), section=_FAST_SECTION
        )

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self.temp_dir.cleanup()

    async def test_heartbeat_and_management_api(self) -> None:
        entries = [{"kind": "executor", "id": "pytest", "provider": "runner"}]
        status, body = await _arequest(
            self.base_url,
            "/api/heartbeat",
            method="POST",
            body=json.dumps(
                _beat_payload(caps_digest=capabilities_digest(entries), capabilities=entries)
            ).encode(),
            content_type="application/json",
        )
        self.assertEqual(status, 200)
        reply = json.loads(body)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["config"], {"interval": 0.3, "timeout": 1.0, "max_backoff": 2.0})
        self.assertFalse(reply["need_caps"])

        # 列表 / 详情（node_id 含中文，走 URL 编码）
        node_url = f"/api/heartbeat/nodes/{quote('从节点-1')}"
        status, body = await _arequest(self.base_url, "/api/heartbeat/nodes")
        self.assertEqual(json.loads(body)["nodes"][0]["status"], "online")

        status, body = await _arequest(self.base_url, node_url)
        detail = json.loads(body)
        self.assertEqual(len(detail["capabilities"]), 1)
        self.assertEqual(detail["history"], [])

        # 跨节点能力查询
        status, body = await _arequest(
            self.base_url, "/api/heartbeat/capabilities?kind=executor&q=pytest"
        )
        self.assertEqual(json.loads(body)["count"], 1)

        # 配置查看（只读）：下发参数 + 本主模块身份
        status, body = await _arequest(self.base_url, "/api/heartbeat/config")
        view = json.loads(body)
        self.assertEqual(view["push"]["interval"], 0.3)
        self.assertEqual(view["master"]["kind"], "master")

        # 移除记录（含历史）
        status, _body = await _arequest(self.base_url, node_url, method="DELETE")
        self.assertEqual(status, 200)
        status, body = await _arequest(self.base_url, "/api/heartbeat/nodes")
        self.assertEqual(json.loads(body)["nodes"], [])

    async def test_errors_and_extension_slots(self) -> None:
        # 缺 node_id → 400 结构化错误
        status, body = await _arequest(
            self.base_url,
            "/api/heartbeat",
            method="POST",
            body=b'{"node_kind": "slave"}',
            content_type="application/json",
        )
        self.assertEqual(status, 400)
        self.assertIn("node_id", json.loads(body)["error"]["message"])

        # 非 JSON → 400（超大 body 的 413 见 BodyLimitTests，直测更确定）
        status, _body = await _arequest(
            self.base_url, "/api/heartbeat", method="POST", body=b"not json"
        )
        self.assertEqual(status, 400)

        # 未知节点 → 404
        status, body = await _arequest(
            self.base_url, f"/api/heartbeat/nodes/{quote('不存在')}"
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

        # 扩展槽契约（与从模块侧 status 接口同款）
        def contribute(response: dict[str, object]) -> None:
            cast(dict[str, object], response["extensions"])["probe"] = {"ok": True}

        self.context.on("heartbeat/collect-nodes", contribute)
        status, body = await _arequest(self.base_url, "/api/heartbeat/nodes")
        response = json.loads(body)
        self.assertEqual(response["extensions"]["probe"], {"ok": True})
        self.assertEqual(response["extension_errors"], [])

    async def test_ui_page_registered_and_unload_cleanup(self) -> None:
        self.assertEqual(len(self.webui.registrations), 1)
        item = self.webui.registrations[0]
        self.assertEqual(item["id"], "nodes")
        self.assertEqual(item["path"], "/nodes")
        self.assertTrue(
            (Path(str(item["resource_root"])) / str(item["entry"])).is_file()
        )

        outcome = self.fiber.dispose()
        if outcome is not None:
            await outcome
        status, _body = await _arequest(self.base_url, "/api/heartbeat/nodes")
        self.assertEqual(status, 404)  # 路由随插件卸载注销（sweeper 同时停止）


class HeartbeatPairTests(unittest.IsolatedAsyncioTestCase):
    """与从模块心跳插件（slave_plugins/heartbeat）对打的全链路集成。"""

    async def asyncSetUp(self) -> None:
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_slave_registers_then_marks_offline(self) -> None:
        root = Path(self.temp_dir.name)
        master, _webui, master_url, _fiber = await _boot_master(
            root / "master", section=_FAST_SECTION
        )
        slave_port = _free_port()
        slave_config = root / "slave" / "config.ini"
        slave_config.parent.mkdir(parents=True, exist_ok=True)
        slave_config.write_text(
            "[node]\nkind = slave\nname = 从节点-1\n\n[web]\n"
            f"port = {slave_port}\n\n[heartbeat]\n"
            f"master_url = {master_url}\ninterval = 0.3\ntimeout = 1.0\nmax_backoff = 2.0\n",
            encoding="utf-8",
        )
        slave: Context = Context()
        slave.provide("node_kind", "slave")
        await slave.plugin(AppConfigPlugin, {"path": str(slave_config)})
        slave.provide(
            "node_name",
            slave.appconfig.section("node").get("name") or socket.gethostname(),
        )
        await slave.plugin(WebApiPlugin)
        await slave.plugin(HeartbeatPlugin)

        def _node_status() -> str:
            status, body = _request(master_url, "/api/heartbeat/nodes")
            if status != 200:
                return ""
            nodes = json.loads(body)["nodes"]
            return str(nodes[0]["status"]) if nodes else ""

        try:
            # 从模块上线：主模块收到心跳并判定在线
            self.assertTrue(
                await wait_until(lambda: _node_status() == "online", timeout=10),
                f"主模块未见到从模块上线：{_node_status()!r}",
            )
            # 从模块停机：主模块按阈值判离线
            await unload_all(slave)
            self.assertTrue(
                await wait_until(lambda: _node_status() == "offline", timeout=10),
                f"主模块未判离线：{_node_status()!r}",
            )
        finally:
            await unload_all(slave)
            await unload_all(master)
