"""M0：AETP V2 协议、Manifest、安全基线和 Schema 契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aetp_protocol import (
    V2_PROTOCOL_VERSION,
    ExecutionAck,
    DesiredPluginVersion,
    MessagePayloadError,
    PluginManifest,
    V2Envelope,
    parse_v2_message,
    parse_v2_topic,
    v2_command_topic,
    v2_event_topic,
)
from aetp_protocol.schema import generate_v2_schemas
from pydantic import ValidationError

GOLDEN_PATH = Path(__file__).parents[1] / "common" / "src" / "aetp_protocol" / "golden_v2.json"
SCHEMA_PATH = Path(__file__).parents[1] / "common" / "src" / "aetp_protocol" / "schema_v2.json"


def _golden() -> dict[str, dict[str, object]]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_v2_golden_messages_parse_as_typed_payloads() -> None:
    golden = _golden()
    envelope, payload = parse_v2_message(golden["node.register"])
    assert envelope.protocol_version == V2_PROTOCOL_VERSION
    assert envelope.message_type == "node.register"
    assert payload.__class__.__name__ == "NodeRegister"

    progress_envelope, progress = parse_v2_message(golden["execution.progress"])
    assert progress_envelope.message_type == "execution.progress"
    assert progress.sequence == 3


def test_v2_manifest_golden_and_point_entrypoint_rule() -> None:
    manifest = PluginManifest.model_validate(_golden()["plugin.manifest"])
    assert manifest.id.root == "org.pytest.executor"
    assert manifest.point.value == "executor"

    invalid = dict(_golden()["plugin.manifest"])
    invalid["entrypoints"] = {"agent": "agent.plugin:create_plugin"}
    with pytest.raises(ValidationError, match="requires entrypoints"):
        PluginManifest.model_validate(invalid)


def test_v2_rejects_old_protocol_and_extra_fields() -> None:
    data = dict(_golden()["execution.progress"])
    data["protocol_version"] = 1
    with pytest.raises(ValidationError):
        V2Envelope.model_validate(data)

    data = dict(_golden()["execution.progress"])
    data["unexpected"] = True
    with pytest.raises(ValidationError):
        V2Envelope.model_validate(data)


def test_v2_rejects_invalid_payload_with_stable_error() -> None:
    data = dict(_golden()["execution.progress"])
    data["payload"] = {"sequence": 0}
    with pytest.raises(MessagePayloadError):
        parse_v2_message(data)


def test_v2_rejected_ack_requires_error_code() -> None:
    with pytest.raises(ValidationError, match="must contain code"):
        ExecutionAck(
            run_id="01J00000000000000000000006",
            shard_id="01J00000000000000000000007",
            attempt_id="01J00000000000000000000008",
            plan_id="01J00000000000000000000005",
            plan_hash="0" * 64,
            accepted=False,
        )


def test_v2_topics_are_distinct_and_strict() -> None:
    event = v2_event_topic("01J00000000000000000000000", "execution.progress")
    command = v2_command_topic("01J00000000000000000000000", "execution.plan")
    assert event.startswith("aetp/v2/")
    assert command.startswith("aetp/v2/")
    assert parse_v2_topic(event).segment == "execution.progress"
    with pytest.raises(ValueError, match="V2"):
        parse_v2_topic(event.replace("aetp/v2", "aetp/v1", 1))
    with pytest.raises(ValueError, match="BusinessId"):
        v2_event_topic("not-a-ulid", "execution.progress")


def test_v2_schema_generation_covers_core_contracts() -> None:
    schemas = generate_v2_schemas()
    assert {"envelope", "plugin_manifest", "node_capability_snapshot", "execution_plan"} <= schemas.keys()
    assert schemas["envelope"]["additionalProperties"] is False


def test_v2_schema_snapshot_is_current() -> None:
    snapshot = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert snapshot == generate_v2_schemas()


def test_desired_plugin_version_is_strict_and_typed() -> None:
    desired = DesiredPluginVersion(
        plugin_id="org.pytest.executor",
        point="executor",
        version="2.0.0",
    )
    assert desired.auto_update is True
    with pytest.raises(ValidationError):
        DesiredPluginVersion(
            plugin_id="org.pytest.executor",
            point="executor",
            version="2.0.0",
            unexpected=True,
        )
