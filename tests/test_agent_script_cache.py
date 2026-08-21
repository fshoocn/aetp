"""P5.6：Agent 脚本下载、sha256 校验与按 hash 本地缓存测试。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import RunAssignPayload
from aetp_protocol.topics import command_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.command_dispatcher import CommandDispatcher
from agent.application.services.script_cache_service import (
    SCRIPT_CHECKSUM_FAILED,
    SCRIPT_DOWNLOAD_FAILED,
    SCRIPT_REF_INVALID,
    ScriptCacheError,
    ScriptCacheService,
    ScriptChecksumError,
    ScriptDownloadError,
)
from agent.config import AgentSettings
from agent.domain.ledger import ScriptCacheEntry
from common.transport import MqttMessage

_DATA = b"aetp-script-package-bytes"
_SHA = hashlib.sha256(_DATA).hexdigest()


def _ledger(tmp_path) -> SQLiteLedger:
    return SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")


def _service(tmp_path, *, fetcher=None) -> ScriptCacheService:
    return ScriptCacheService(
        tmp_path / "scripts",
        _ledger(tmp_path),
        fetcher=fetcher,
    )


def _script_ref(sha256: str = _SHA, download_url: str | None = None) -> dict:
    return {
        "script_id": "S-1",
        "version": 1,
        "sha256": sha256,
        "download_url": download_url or "https://master.example/scripts/S-1",
    }


def test_ensure_cached_downloads_verifies_and_caches(tmp_path) -> None:
    calls = []
    service = _service(
        tmp_path,
        fetcher=lambda url: calls.append(url) or _DATA,
    )

    entry = service.ensure_cached(_script_ref())

    assert entry.script_id == "S-1"
    assert entry.version == 1
    assert entry.sha256 == _SHA
    assert len(calls) == 1
    assert (tmp_path / "scripts" / _SHA / "S-1-v1.bin").read_bytes() == _DATA

    cached = _ledger(tmp_path).get_cached_script("S-1", 1, _SHA)
    assert cached is not None
    assert cached.path == str(tmp_path / "scripts" / _SHA / "S-1-v1.bin")

    # 第二次命中缓存，不再下载
    service.ensure_cached(_script_ref())
    assert len(calls) == 1


def test_checksum_mismatch_raises_and_does_not_cache(tmp_path) -> None:
    service = _service(tmp_path, fetcher=lambda url: _DATA)
    bad_ref = _script_ref(sha256="0" * 64)

    with pytest.raises(ScriptChecksumError) as exc_info:
        service.ensure_cached(bad_ref)
    assert exc_info.value.code == SCRIPT_CHECKSUM_FAILED

    # 校验失败不落缓存：账本与磁盘都没有痕迹
    assert _ledger(tmp_path).get_cached_script("S-1", 1, "0" * 64) is None
    assert not (tmp_path / "scripts").exists() or not any((tmp_path / "scripts").rglob("*.bin"))


def test_download_failure_raises(tmp_path) -> None:
    service = _service(
        tmp_path,
        fetcher=lambda url: (_ for _ in ()).throw(OSError("connection reset")),
    )

    with pytest.raises(ScriptDownloadError) as exc_info:
        service.ensure_cached(_script_ref())
    assert exc_info.value.code == SCRIPT_DOWNLOAD_FAILED


def test_invalid_script_ref_raises(tmp_path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ScriptCacheError) as exc_info:
        service.ensure_cached({"script_id": "S-1"})
    assert exc_info.value.code == SCRIPT_REF_INVALID


def test_cache_hit_reuses_existing_entry_without_download(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    path = tmp_path / "scripts" / _SHA / "S-1-v1.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(_DATA)
    ledger.cache_script(ScriptCacheEntry("S-1", 1, _SHA, str(path)))

    calls = []
    service = ScriptCacheService(tmp_path / "scripts", ledger, fetcher=lambda url: calls.append(url) or _DATA)
    entry = service.ensure_cached(_script_ref())

    assert entry.path == str(path)
    assert calls == []


# -----------------------------------------------------------------------
# CommandDispatcher 集成：脚本准备失败回 ACK(rejected)，可重试
# -----------------------------------------------------------------------


def _dispatcher(tmp_path, fetcher) -> tuple[CommandDispatcher, SQLiteLedger]:
    settings = AgentSettings(
        node_id="bench-001",
        name="bench",
        master_id="aetp-master",
        mqtt_client_id="aetp-agent-bench-001",
        mqtt_use_tls=False,
    )
    ledger = _ledger(tmp_path)
    dispatcher = CommandDispatcher(
        settings,
        ledger,
        is_registered=lambda: True,
        script_cache=ScriptCacheService(tmp_path / "scripts", ledger, fetcher=fetcher),
    )
    return dispatcher, ledger


def _assign_envelope(sha256: str = _SHA) -> Envelope:
    payload = RunAssignPayload(
        project_id="p1",
        task_id="T-1",
        shard_id="SH-1",
        shard_index=0,
        run_id="R-script",
        attempt_no=1,
        dispatch_id="D-1",
        task_type="can_test",
        plugin_version="1.0.0",
        script_ref=_script_ref(sha256=sha256),
        case_keys=["case-1"],
    )
    return Envelope(
        message_id="assign-script-1",
        message_type=MessageType.RUN_ASSIGN.value,
        sent_at="2026-08-17T12:00:00Z",
        sender=Sender(kind=SenderKind.MASTER, id="aetp-master", session_id="master-sess"),
        trace_id="R-script",
        payload=payload.model_dump(mode="json"),
    )


def _message(envelope: Envelope) -> MqttMessage:
    return MqttMessage(
        topic=command_topic("bench-001", "assign"),
        payload=envelope.model_dump_json().encode("utf-8"),
    )


def _now() -> datetime:
    return datetime(2099, 1, 1, tzinfo=UTC).replace(tzinfo=None)


def test_assign_script_download_failure_rejects_then_retry_succeeds(tmp_path) -> None:
    state = {"available": False}

    def fetcher(url: str) -> bytes:
        if not state["available"]:
            raise OSError("connection refused")
        return _DATA

    dispatcher, ledger = _dispatcher(tmp_path, fetcher)
    env = _assign_envelope()
    message = _message(env)

    assert dispatcher.handle_command(message) is True
    rejected = ledger.claim_due_outbox(10, _now())
    assert len(rejected) == 1
    assert rejected[0].payload["payload"]["accepted"] is False
    assert SCRIPT_DOWNLOAD_FAILED in rejected[0].payload["payload"]["reason"]
    assert ledger.get_run("R-script") is None

    # 修复下载后重试同一消息：成功 claim 并回 accepted ACK
    state["available"] = True
    assert dispatcher.handle_command(message) is True
    accepted = ledger.claim_due_outbox(10, _now())
    assert accepted[0].payload["payload"]["accepted"] is True
    assert ledger.get_run("R-script") is not None


def test_assign_script_checksum_mismatch_rejected_and_not_cached(tmp_path) -> None:
    dispatcher, ledger = _dispatcher(tmp_path, fetcher=lambda url: _DATA)
    message = _message(_assign_envelope(sha256="0" * 64))

    assert dispatcher.handle_command(message) is True
    rejected = ledger.claim_due_outbox(10, _now())
    assert rejected[0].payload["payload"]["accepted"] is False
    assert SCRIPT_CHECKSUM_FAILED in rejected[0].payload["payload"]["reason"]
    assert ledger.get_run("R-script") is None
    assert ledger.get_cached_script("S-1", 1, "0" * 64) is None
