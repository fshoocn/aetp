"""P5.7：Agent 脚本辅助预检/解析服务测试。

验收要点：
1. script.verify → verify-result 回传（verify_id 幂等）
2. script.parse → parse-result 回传（parse_id 幂等，含 case 事实）
3. 插件未声明台架侧能力时回传 PLUGIN_NOT_FOUND
4. 未注册时拒绝处理
5. 脚本下载失败/校验失败回传错误（不落缓存）
6. 主用例索引仍由 Master 生成（Agent 只回传事实，不写 script_cases）
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import pytest

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import ScriptParsePayload, ScriptVerifyPayload
from aetp_protocol.plugin import CaseInfo
from aetp_protocol.topics import command_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.script_cache_service import ScriptCacheService
from agent.application.services.script_preflight_service import (
    ScriptPreflightService,
)
from agent.config import AgentSettings
from agent.plugins.execution import AgentPluginRegistry
from common.transport import MqttMessage

_DATA = b"aetp-script-content"
_SHA = hashlib.sha256(_DATA).hexdigest()


def _now() -> datetime:
    return datetime(2099, 1, 1, tzinfo=timezone.utc)


_SETTINGS = AgentSettings(
    node_id="bench-001",
    name="bench",
    master_id="aetp-master",
    mqtt_host="broker.test",
    mqtt_port=1883,
    mqtt_client_id="aetp-agent-bench-001",
    mqtt_use_tls=False,
)


class AgentSidePlugin:
    """声明台架侧验证 + 解析能力的 Agent 插件。"""

    task_type = "canoe"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    display_name = "CANoe"
    verify_location = "agent"
    parse_location = "agent"

    def verify_script(self, script_dir, config):
        if config.get("broken"):
            return ["COM 加载失败"]
        return []

    def parse_cases(self, script_dir, config):
        return [
            CaseInfo(stable_key="case-1", name="Case 1"),
            CaseInfo(stable_key="case-2", name="Case 2", parent_path="suite"),
        ]


class MasterOnlyPlugin:
    """未声明台架侧能力的插件（默认 master 位置）。"""

    task_type = "pytest"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    display_name = "pytest"


def _service(tmp_path, *, registry, registered=True, fetcher=None) -> ScriptPreflightService:
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    script_cache = ScriptCacheService(
        tmp_path / "scripts", ledger, fetcher=fetcher or (lambda url: _DATA)
    )
    return ScriptPreflightService(
        _SETTINGS,
        ledger,
        script_cache,
        plugin_registry=registry,
        is_registered=lambda: registered,
        session_id=lambda: "sess-1",
        now=_now,
    ), ledger


def _command_message(envelope: Envelope, segment: str) -> MqttMessage:
    return MqttMessage(
        topic=command_topic("bench-001", segment),
        payload=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
    )


def _verify_envelope(
    *, verify_id="V-1", config=None, script_id="S-1", task_type="canoe"
) -> Envelope:
    payload = ScriptVerifyPayload(
        verify_id=verify_id,
        script_id=script_id,
        version=1,
        task_type=task_type,
        plugin_version="1.0.0",
        script_ref={"script_id": script_id, "version": 1, "sha256": _SHA, "download_url": "https://master/scripts/S-1"},
        config=config or {},
    )
    return Envelope(
        message_id=uuid.uuid4().hex,
        message_type=MessageType.SCRIPT_VERIFY.value,
        sent_at=_now(),
        sender=Sender(kind=SenderKind.MASTER, id="aetp-master", session_id="master-sess"),
        trace_id="bench-001",
        payload=payload.model_dump(mode="json"),
    )


def _parse_envelope(
    *, parse_id="P-1", config=None, script_id="S-1", task_type="canoe"
) -> Envelope:
    payload = ScriptParsePayload(
        parse_id=parse_id,
        script_id=script_id,
        version=1,
        task_type=task_type,
        plugin_version="1.0.0",
        script_ref={"script_id": script_id, "version": 1, "sha256": _SHA, "download_url": "https://master/scripts/S-1"},
        config=config or {},
    )
    return Envelope(
        message_id=uuid.uuid4().hex,
        message_type=MessageType.SCRIPT_PARSE.value,
        sent_at=_now(),
        sender=Sender(kind=SenderKind.MASTER, id="aetp-master", session_id="master-sess"),
        trace_id="bench-001",
        payload=payload.model_dump(mode="json"),
    )


def _claim_result(ledger, segment: str) -> Envelope:
    pending = ledger.claim_due_outbox(10, _now().replace(tzinfo=None))
    for entry in pending:
        env = Envelope.model_validate(entry.payload)
        if env.message_type == f"script.{segment}":
            return env
    raise AssertionError(f"未找到 script.{segment} 结果")


# -----------------------------------------------------------------------
# verify 命令
# -----------------------------------------------------------------------

def test_verify_ok_returns_empty_errors(tmp_path) -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(AgentSidePlugin())
    service, ledger = _service(tmp_path, registry=registry)

    env = _verify_envelope()
    assert service.handle_verify(command_topic("bench-001", "verify"), env) is True

    result = _claim_result(ledger, "verify-result")
    assert result.payload["verify_id"] == "V-1"
    assert result.payload["script_id"] == "S-1"
    assert result.payload["errors"] == []


def test_verify_with_errors_returns_errors(tmp_path) -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(AgentSidePlugin())
    service, ledger = _service(tmp_path, registry=registry)

    env = _verify_envelope(config={"broken": True})
    assert service.handle_verify(command_topic("bench-001", "verify"), env) is True

    result = _claim_result(ledger, "verify-result")
    assert result.payload["errors"] == ["COM 加载失败"]


def test_verify_rejected_when_not_registered(tmp_path) -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(AgentSidePlugin())
    service, ledger = _service(tmp_path, registry=registry, registered=False)

    env = _verify_envelope()
    assert service.handle_verify(command_topic("bench-001", "verify"), env) is False
    assert ledger.claim_due_outbox(10, _now().replace(tzinfo=None)) == []


def test_verify_plugin_without_agent_capability_returns_error(tmp_path) -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(MasterOnlyPlugin())
    service, ledger = _service(tmp_path, registry=registry)

    env = _verify_envelope(task_type="pytest")
    assert service.handle_verify(command_topic("bench-001", "verify"), env) is True

    result = _claim_result(ledger, "verify-result")
    assert any("PLUGIN_NOT_FOUND" in e for e in result.payload["errors"])


# -----------------------------------------------------------------------
# parse 命令
# -----------------------------------------------------------------------

def test_parse_ok_returns_cases(tmp_path) -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(AgentSidePlugin())
    service, ledger = _service(tmp_path, registry=registry)

    env = _parse_envelope()
    assert service.handle_parse(command_topic("bench-001", "parse"), env) is True

    result = _claim_result(ledger, "parse-result")
    assert result.payload["parse_id"] == "P-1"
    assert result.payload["script_id"] == "S-1"
    assert result.payload["errors"] == []
    cases = result.payload["cases"]
    assert [c["stable_key"] for c in cases] == ["case-1", "case-2"]
    assert cases[1]["parent_path"] == "suite"


def test_parse_is_idempotent_by_parse_id(tmp_path) -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(AgentSidePlugin())
    service, ledger = _service(tmp_path, registry=registry)

    env = _parse_envelope()
    topic = command_topic("bench-001", "parse")
    assert service.handle_parse(topic, env) is True

    # 同一 message_id 第二次：inbox 去重，仍回传同一稳定 outbox 结果
    assert service.handle_parse(topic, env) is True
    pending = ledger.claim_due_outbox(10, _now().replace(tzinfo=None))
    parse_results = [
        Envelope.model_validate(e.payload)
        for e in pending
        if Envelope.model_validate(e.payload).message_type == "script.parse-result"
    ]
    assert len(parse_results) == 1  # 同一 parse_id 只保留一个 outbox 结果


def test_parse_plugin_without_agent_capability_returns_error(tmp_path) -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(MasterOnlyPlugin())
    service, ledger = _service(tmp_path, registry=registry)

    env = _parse_envelope(task_type="pytest")
    assert service.handle_parse(command_topic("bench-001", "parse"), env) is True

    result = _claim_result(ledger, "parse-result")
    assert any("PLUGIN_NOT_FOUND" in e for e in result.payload["errors"])


# -----------------------------------------------------------------------
# 下载/校验失败
# -----------------------------------------------------------------------

def test_parse_download_failure_returns_error(tmp_path) -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(AgentSidePlugin())

    def fetcher(url):
        raise OSError("connection refused")

    service, ledger = _service(tmp_path, registry=registry, fetcher=fetcher)

    env = _parse_envelope()
    assert service.handle_parse(command_topic("bench-001", "parse"), env) is True

    result = _claim_result(ledger, "parse-result")
    assert any("SCRIPT_DOWNLOAD_FAILED" in e for e in result.payload["errors"])
    # 校验失败不落缓存
    assert ledger.get_cached_script("S-1", 1, _SHA) is None
