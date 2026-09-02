"""Agent 统一结构化日志门面和标准 logging Handler。"""

from __future__ import annotations

import json
import logging
import re
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aetp_protocol.ids import BusinessId, PluginId, RequestId, SemVer, SessionId, TraceId
from aetp_protocol.logs import (
    AgentLogBatch,
    ExceptionInfo,
    LogCode,
    LogContext,
    LogEvent,
    LogLevel,
)
from aetp_protocol.payloads import AgentLogReceived

from agent.config import AgentSettings
from agent.domain.ledger import Ledger

_LEVELS = {
    LogLevel.DEBUG: logging.DEBUG,
    LogLevel.INFO: logging.INFO,
    LogLevel.WARN: logging.WARNING,
    LogLevel.ERROR: logging.ERROR,
}
_COMPONENT_RE = re.compile(r"[^a-z0-9_.-]+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?i)(password|passwd|token|secret|authorization|api[_-]?key)=([^\s,;]+)"
)


class AgentLogFacade(logging.Handler):
    """把标准 logging 记录写入 JSONL 和本地 AgentLog spool。"""

    def __init__(
        self,
        settings: AgentSettings,
        ledger: Ledger,
        *,
        default_level: str | None = None,
    ) -> None:
        super().__init__(level=logging.NOTSET)
        self._settings = settings
        self._ledger = ledger
        self._default_level = _parse_logging_level(default_level or settings.log_level)
        self._component_levels: dict[str, tuple[int, datetime | None]] = {}
        self._plugin_levels: dict[str, tuple[int, datetime | None]] = {}
        self._log_path = Path(
            settings.structured_log_file
            or Path(settings.log_file).with_suffix(".jsonl")
        )

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__ or record.name.startswith(f"{__name__}."):
            return
        try:
            context = _build_context(record)
            plugin_key = context.plugin_id.root if context.plugin_id is not None else None
            minimum = self._minimum_level(record.name, plugin_key)
            if record.levelno < minimum:
                return
            sequence = self._ledger.next_agent_log_sequence()
            message = _redact_text(record.getMessage())
            exception_type = (
                record.exc_info[0]
                if record.exc_info is not None and record.exc_info[0] is not None
                else Exception
            )
            exception_value = record.exc_info[1] if record.exc_info is not None else None
            event = LogEvent(
                event_id=BusinessId(_new_event_id()),
                source="agent",
                source_id=self._settings.node_id,
                sequence=sequence,
                occurred_at=datetime.now(UTC),
                level=_to_log_level(record.levelno),
                component=_component(record.name),
                event_code=LogCode(f"agent.{_component(record.name)}.log"[:128]),
                message_template=_redact_text(record.msg if isinstance(record.msg, str) else message),
                message=message,
                context=context,
                detail=_json_object(getattr(record, "aetp_detail", {})),
                exception=(
                    ExceptionInfo(
                        type_name=exception_type.__name__,
                        message=_redact_text(str(exception_value)) if exception_value is not None else message,
                        stack_trace=_redact_text("".join(traceback.format_exception(*record.exc_info)))
                        if record.exc_info
                        else None,
                    )
                    if record.exc_info
                    else None
                ),
            )
            self._ledger.append_agent_log(event)
            self._append_jsonl(event)
        except Exception:
            self.handleError(record)

    def update_level(
        self,
        component: str,
        level: LogLevel,
        *,
        plugin_id: PluginId | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        """更新组件或插件级过滤阈值。"""
        target = self._plugin_levels if plugin_id is not None else self._component_levels
        key = plugin_id.root if plugin_id is not None else _component(component)
        target[key] = (_LEVELS[level], expires_at)
        logging.getLogger(key).setLevel(logging.DEBUG)

    def build_batch(self, session_id: SessionId, limit: int = 100) -> AgentLogBatch | None:
        entries = self._ledger.list_pending_agent_logs(limit)
        if not entries:
            return None
        events = tuple(entry.event for entry in entries)
        return AgentLogBatch(
            node_id=BusinessId(self._settings.node_id),
            session_id=session_id,
            first_sequence=events[0].sequence,
            events=events,
        )

    def acknowledge(self, receipt: AgentLogReceived) -> int:
        if not receipt.accepted:
            return 0
        return self._ledger.acknowledge_agent_logs(
            receipt.session_id.root,
            receipt.first_sequence,
            receipt.last_sequence,
        )

    def pending_count(self) -> int:
        return len(self._ledger.list_pending_agent_logs(1000))

    def _minimum_level(self, component: str, plugin_id: str | None) -> int:
        now = datetime.now(UTC)
        if plugin_id is not None:
            override = self._plugin_levels.get(plugin_id)
            if override is not None and (override[1] is None or override[1] > now):
                return override[0]
        override = self._component_levels.get(_component(component))
        if override is not None and (override[1] is None or override[1] > now):
            return override[0]
        return self._default_level

    def _append_jsonl(self, event: LogEvent) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (event.model_dump_json() + "\n").encode("utf-8")
        if (
            self._log_path.exists()
            and self._log_path.stat().st_size + len(encoded) > self._settings.structured_log_max_bytes
        ):
            self._rotate_jsonl()
        with self._log_path.open("a", encoding="utf-8") as stream:
            stream.write(encoded.decode("utf-8"))

    def _rotate_jsonl(self) -> None:
        backup_count = max(0, self._settings.structured_log_backup_count)
        if backup_count == 0:
            self._log_path.unlink(missing_ok=True)
            return
        oldest = Path(f"{self._log_path}.{backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(backup_count - 1, 0, -1):
            source = Path(f"{self._log_path}.{index}")
            if source.exists():
                source.replace(Path(f"{self._log_path}.{index + 1}"))
        if self._log_path.exists():
            self._log_path.replace(Path(f"{self._log_path}.1"))


def _build_context(record: logging.LogRecord) -> LogContext:
    raw = getattr(record, "aetp_context", {})
    if not isinstance(raw, Mapping):
        raw = {}
    return LogContext(
        request_id=_optional_model(raw.get("request_id"), RequestId),
        trace_id=_optional_model(raw.get("trace_id"), TraceId),
        node_id=_optional_model(raw.get("node_id"), BusinessId),
        project_id=_optional_model(raw.get("project_id"), BusinessId),
        run_id=_optional_model(raw.get("run_id"), BusinessId),
        attempt_id=_optional_model(raw.get("attempt_id"), BusinessId),
        plan_id=_optional_model(raw.get("plan_id"), BusinessId),
        plugin_id=_optional_model(raw.get("plugin_id"), PluginId),
        plugin_version=_optional_model(raw.get("plugin_version"), SemVer),
    )


def _optional_model(value: object, model):
    if value is None or isinstance(value, model):
        return value
    try:
        return model(value)
    except (TypeError, ValueError):
        return None


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    try:
        encoded = json.loads(json.dumps(_redact_json(value), ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {}
    return encoded if isinstance(encoded, dict) else {}


def _redact_json(value: object, *, key: str = "") -> object:
    if key.lower() in {"password", "passwd", "token", "secret", "authorization", "api_key"}:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): _redact_json(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_json(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _redact_text(value) if isinstance(value, str) else value
    return str(value)


def _component(value: str) -> str:
    normalized = _COMPONENT_RE.sub(".", value.lower()).strip(".") or "agent"
    return normalized[:128]


def _redact_text(value: str) -> str:
    return _SECRET_RE.sub(r"\1=[REDACTED]", value)


def _parse_logging_level(value: str) -> int:
    normalized = value.strip().lower()
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }.get(normalized, logging.INFO)


def _to_log_level(levelno: int) -> LogLevel:
    if levelno >= logging.ERROR:
        return LogLevel.ERROR
    if levelno >= logging.WARNING:
        return LogLevel.WARN
    if levelno <= logging.DEBUG:
        return LogLevel.DEBUG
    return LogLevel.INFO


def _new_event_id() -> str:
    from aetp_protocol.ids import new_ulid

    return new_ulid()


__all__ = ["AgentLogFacade"]
