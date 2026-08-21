"""Golden messages（P4.1）。

合法消息样例（Envelope dict / 完整 Envelope），供契约测试与文档参考：
- 保证 Envelope 可解析、校验通过
- 作为 topic/sender 匹配校验的基准数据
"""

from __future__ import annotations

from aetp_protocol.envelope import PROTOCOL_VERSION, Envelope

_MESSAGE_ID = "01900000000000000000000001"
_SESSION_ID = "01900000000000000000000002"
_TRACE_ID = "01900000000000000000000003"
_SENT_AT = "2026-08-11T08:00:00.000Z"


def _base(message_type: str, sender_kind: str, sender_id: str) -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": _MESSAGE_ID,
        "message_type": message_type,
        "sent_at": _SENT_AT,
        "sender": {"kind": sender_kind, "id": sender_id, "session_id": _SESSION_ID},
        "correlation_id": None,
        "trace_id": _TRACE_ID,
        "payload": {},
    }


# Agent -> Master：节点注册（topic: aetp/v1/agents/bench-001/events/register）
GOLDEN_NODE_REGISTER: dict = _base("node.register", "agent", "bench-001")
GOLDEN_NODE_REGISTER["payload"] = {
    "node_id": "bench-001",
    "name": "Bench 1",
    "capabilities": {
        "vehicle": {
            "vendors": [
                {
                    "name": "vector",
                    "buses": [
                        {
                            "bus_type": "can",
                            "channels": [
                                {"name": "can0", "enabled": True},
                                {"name": "can1", "enabled": True},
                            ],
                        },
                        {
                            "bus_type": "lin",
                            "channels": [{"name": "lin0", "enabled": True}],
                        },
                    ],
                }
            ]
        },
        "system": {
            "operating_system": {
                "name": "windows",
                "version": "10.0.19045",
            },
            "memory_mb": 16384,
            "cpu_cores": 8,
        },
    },
    "tags": ["can", "bench"],
    "supported_versions": {"can_test": ["1.0.0"]},
    "plugin_versions": {"can_test": "1.0.0"},
}

# Agent -> Master：心跳（topic: aetp/v1/agents/bench-001/events/heartbeat）
GOLDEN_NODE_HEARTBEAT: dict = _base("node.heartbeat", "agent", "bench-001")
GOLDEN_NODE_HEARTBEAT["payload"] = {
    "node_id": "bench-001",
    "status": "online",
    "load": {"running_shards": 1, "queued_shards": 0},
    "active_run_ids": ["R-1"],
}

# Master -> Agent：Shard 派发（topic: aetp/v1/master/agents/bench-001/commands/assign）
GOLDEN_RUN_ASSIGN: dict = _base("run.assign", "master", "master-01")
GOLDEN_RUN_ASSIGN["payload"] = {
    "project_id": "p1",
    "task_id": "T-1",
    "shard_id": "SH-1",
    "shard_index": 0,
    "run_id": "R-1",
    "attempt_no": 1,
    "device_allocations": [],
    "dispatch_id": "D-1",
    "task_type": "can_test",
    "plugin_version": "1.0.0",
    "script_ref": {
        "script_id": "S-1",
        "version": 1,
        "sha256": "a" * 64,
        "download_url": "http://127.0.0.1:8000/api/v1/internal/scripts/S-1/download",
    },
    "case_keys": ["can_open_channel", "can_send_frame"],
    "execution_params": {"channel": 0},
    "timeout_s": 1800,
}

# Agent -> Master：ACK（topic: aetp/v1/agents/bench-001/events/ack）
GOLDEN_RUN_ACK: dict = _base("run.ack", "agent", "bench-001")
GOLDEN_RUN_ACK["payload"] = {
    "run_id": "R-1",
    "attempt_no": 1,
    "dispatch_id": "D-1",
    "accepted": True,
    "reason": "ok",
}

# Master -> Agent：脚本辅助解析命令（P5.7，topic: .../commands/parse）
GOLDEN_SCRIPT_PARSE: dict = _base("script.parse", "master", "master-01")
GOLDEN_SCRIPT_PARSE["payload"] = {
    "parse_id": "P-1",
    "script_id": "S-1",
    "version": 1,
    "task_type": "canoe",
    "plugin_version": "1.0.0",
    "script_ref": {
        "script_id": "S-1",
        "version": 1,
        "sha256": "a" * 64,
        "download_url": "http://127.0.0.1:8000/api/v1/internal/scripts/S-1/download",
    },
    "config": {},
}

# Agent -> Master：脚本辅助解析结果（P5.7，topic: .../events/parse-result）
GOLDEN_SCRIPT_PARSE_RESULT: dict = _base("script.parse-result", "agent", "bench-001")
GOLDEN_SCRIPT_PARSE_RESULT["payload"] = {
    "parse_id": "P-1",
    "script_id": "S-1",
    "cases": [{"stable_key": "case-1", "name": "Case 1", "parent_path": "", "tags": [], "params": {}}],
    "errors": [],
}

# Master -> Agent：脚本验证命令（verify_location=agent，topic: .../commands/verify）
GOLDEN_SCRIPT_VERIFY: dict = _base("script.verify", "master", "master-01")
GOLDEN_SCRIPT_VERIFY["payload"] = {
    "verify_id": "V-1",
    "script_id": "S-1",
    "version": 1,
    "task_type": "canoe",
    "plugin_version": "1.0.0",
    "script_ref": {
        "script_id": "S-1",
        "version": 1,
        "sha256": "a" * 64,
        "download_url": "http://127.0.0.1:8000/api/v1/internal/scripts/S-1/download",
    },
    "config": {},
}

# Agent -> Master：脚本验证结果（verify_location=agent，topic: .../events/verify-result）
GOLDEN_SCRIPT_VERIFY_RESULT: dict = _base("script.verify-result", "agent", "bench-001")
GOLDEN_SCRIPT_VERIFY_RESULT["payload"] = {
    "verify_id": "V-1",
    "script_id": "S-1",
    "errors": [],
}

# 完整 Envelope 实例（解析/校验通过）
GOLDEN_ENVELOPES: dict[str, Envelope] = {
    "node.register": Envelope.model_validate(GOLDEN_NODE_REGISTER),
    "node.heartbeat": Envelope.model_validate(GOLDEN_NODE_HEARTBEAT),
    "run.assign": Envelope.model_validate(GOLDEN_RUN_ASSIGN),
    "run.ack": Envelope.model_validate(GOLDEN_RUN_ACK),
    "script.parse": Envelope.model_validate(GOLDEN_SCRIPT_PARSE),
    "script.parse-result": Envelope.model_validate(GOLDEN_SCRIPT_PARSE_RESULT),
    "script.verify": Envelope.model_validate(GOLDEN_SCRIPT_VERIFY),
    "script.verify-result": Envelope.model_validate(GOLDEN_SCRIPT_VERIFY_RESULT),
}
