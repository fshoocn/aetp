"""Agent 脚本辅助预检/解析编排（P5.7，D-17，§9.8）。

常规脚本的验证与用例解析由 Master 面插件完成；只有当插件声明依赖台架
运行时环境（``verify_location="agent"`` / ``parse_location="agent"``）时，
Master 才通过 ``script.verify`` / ``script.parse`` 命令请求 Agent 辅助执行。

本服务只做**受控辅助**：

1. 下载并校验脚本（复用 P5.6 ``ScriptCacheService``），解包到临时 ``script_dir``；
2. 调用同一插件包 Agent 入口的 ``verify_script`` / ``parse_cases``；
3. 把**事实结果**（错误列表 / 用例列表）写入 outbox 回传 Master；
4. ``verify-result`` 按 ``verify_id``、``parse-result`` 按 ``parse_id`` 幂等。

Agent 不写 ``script_cases``、不判定 ``parse_status``——主用例索引与可用性
最终仍由 Master 校验后决定（D-17）。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    ScriptParsePayload,
    ScriptParseResultPayload,
    ScriptVerifyPayload,
    ScriptVerifyResultPayload,
)
from aetp_protocol.topics import event_topic, parse_topic

from agent.application.services.script_archive import extract_zip_safely
from agent.application.services.script_cache_service import (
    ScriptCacheError,
    ScriptCacheService,
)
from agent.config import AgentSettings
from agent.domain.ledger import Ledger
from agent.plugins.errors import (
    PluginNotFoundError,
    PluginVersionMismatchError,
)

if TYPE_CHECKING:
    from agent.plugins import AgentPluginRegistry

logger = logging.getLogger(__name__)

SCRIPT_DIR_INVALID = "SCRIPT_DIR_INVALID"


class ScriptPreflightError(ValueError):
    """脚本预检/解析错误（携带机器可读错误码，回传 Master）。"""

    code = "SCRIPT_PREFLIGHT_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ScriptPreflightService:
    """下载脚本、调用 Agent 插件预检/解析并回传结果。"""

    def __init__(
        self,
        settings: AgentSettings,
        ledger: Ledger,
        script_cache: ScriptCacheService,
        plugin_registry: AgentPluginRegistry | None = None,
        *,
        is_registered: Callable[[], bool] | None = None,
        session_id: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._script_cache = script_cache
        self._plugin_registry = plugin_registry
        self._is_registered = is_registered or (lambda: True)
        self._session_id = session_id or (lambda: self._settings.node_id)
        self._now = now or (lambda: datetime.now(UTC))

    # -- 入口 ---------------------------------------------------------------

    def handle_verify(self, topic: str, envelope: Envelope) -> bool:
        """处理 script.verify 命令，回传 script.verify-result。"""
        return self._handle(
            topic,
            envelope,
            MessageType.SCRIPT_VERIFY,
            self._run_verify,
        )

    def handle_parse(self, topic: str, envelope: Envelope) -> bool:
        """处理 script.parse 命令，回传 script.parse-result。"""
        return self._handle(
            topic,
            envelope,
            MessageType.SCRIPT_PARSE,
            self._run_parse,
        )

    # -- 通用处理骨架 -------------------------------------------------------

    def _handle(
        self,
        topic: str,
        envelope: Envelope,
        expected_type: MessageType,
        runner: Callable[..., list[dict] | list[str]],
    ) -> bool:
        """通用：注册检查 → 目标校验 → 去重 → 下载解包 → 插件执行 → 回传。"""
        if not self._is_registered():
            logger.warning("脚本预检被拒绝：Agent 未注册")
            return False

        topic_info = parse_topic(topic)
        if topic_info.node_id != self._settings.node_id:
            logger.warning(
                "脚本预检目标节点不匹配: expected=%s got=%s",
                self._settings.node_id,
                topic_info.node_id,
            )
            return False

        # 去重：已处理的命令静默忽略（结果已在 outbox，稳定逻辑 ID 幂等重发）。
        if not self._ledger.record_inbox(
            origin_id=envelope.sender.id,
            message_id=envelope.message_id,
            message_type=envelope.message_type,
        ):
            logger.debug(
                "脚本预检幂等忽略（inbox 去重）: message_id=%s",
                envelope.message_id,
            )
            return True

        script_id = str(envelope.payload.get("script_id", ""))
        script_dir: Path | None = None
        try:
            script_id, _version, task_type, plugin_version, script_ref, config = (
                self._parse_command_payload(envelope, expected_type)
            )
            plugin = self._require_agent_plugin(task_type, plugin_version)
            script_dir = self._materialize_script_dir(script_ref)
            result = runner(script_dir, plugin, config)
        except ScriptPreflightError as exc:
            logger.warning("脚本预检失败: %s", exc)
            self._enqueue_error_result(envelope, script_id, errors=[f"{exc.code}: {exc}"])
            return True
        except ScriptCacheError as exc:
            logger.warning("脚本预检失败（下载/校验）: %s", exc)
            self._enqueue_error_result(envelope, script_id, errors=[f"{exc.code}: {exc}"])
            return True
        except Exception as exc:
            logger.exception("脚本预检执行异常: script_id=%s", script_id)
            self._enqueue_error_result(
                envelope,
                script_id,
                errors=[f"SCRIPT_VERIFY_FAILED: {type(exc).__name__}: {exc}"],
            )
            return True
        finally:
            if script_dir is not None:
                shutil.rmtree(script_dir, ignore_errors=True)

        if expected_type is MessageType.SCRIPT_VERIFY:
            errors: list[str] = [e for e in result if isinstance(e, str)]
            self._enqueue_verify_result(envelope, script_id, errors=errors)
        else:
            cases: list[dict] = [e for e in result if isinstance(e, dict)]
            self._enqueue_parse_result(envelope, script_id, cases=cases)
        return True

    # -- payload 解析 -------------------------------------------------------

    def _parse_command_payload(
        self, envelope: Envelope, expected_type: MessageType
    ) -> tuple[str, int, str, str, dict, dict]:
        """解析命令 payload，返回 (script_id, version, task_type, plugin_version, script_ref, config)。"""
        try:
            if expected_type is MessageType.SCRIPT_VERIFY:
                payload = ScriptVerifyPayload.model_validate(envelope.payload)
                return (
                    payload.script_id,
                    payload.version,
                    payload.task_type,
                    payload.plugin_version,
                    payload.script_ref,
                    payload.config,
                )
            payload = ScriptParsePayload.model_validate(envelope.payload)
            return (
                payload.script_id,
                payload.version,
                payload.task_type,
                payload.plugin_version,
                payload.script_ref,
                payload.config,
            )
        except Exception as exc:
            raise ScriptPreflightError(
                f"脚本预检 payload 校验失败: {exc}",
                code="SCRIPT_REF_INVALID",
            ) from exc

    # -- 插件选择 -----------------------------------------------------------

    def _require_agent_plugin(self, task_type: str, plugin_version: str):
        """取兼容插件实例；缺失/版本不符抛错（回传 Master）。"""
        if self._plugin_registry is None:
            raise ScriptPreflightError(
                "Agent 未装配插件注册表", code="PLUGIN_NOT_FOUND"
            )
        try:
            return self._plugin_registry.require_compatible(
                task_type, plugin_version
            )
        except (PluginNotFoundError, PluginVersionMismatchError) as exc:
            code = (
                "PLUGIN_NOT_FOUND"
                if isinstance(exc, PluginNotFoundError)
                else "PLUGIN_VERSION_MISMATCH"
            )
            raise ScriptPreflightError(str(exc), code=code) from exc

    # -- 脚本物化 -----------------------------------------------------------

    def _materialize_script_dir(self, script_ref: dict) -> Path:
        """下载校验后解包脚本，返回供插件读取的临时目录（调用方负责清理）。"""
        entry = self._script_cache.ensure_cached(script_ref)
        source = Path(entry.path)
        if not source.is_file():
            raise ScriptPreflightError(
                f"缓存脚本文件缺失: {source}", code="SCRIPT_REF_INVALID"
            )
        tmp_dir = Path(tempfile.mkdtemp(prefix="aetp-script-"))
        try:
            if zipfile.is_zipfile(source):
                extract_zip_safely(source, tmp_dir)
            else:
                shutil.copy2(source, tmp_dir / "test_script.py")
        except Exception as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise ScriptPreflightError(
                f"脚本解包失败: {exc}", code="SCRIPT_DIR_INVALID"
            ) from exc
        return tmp_dir

    # -- 插件执行 -----------------------------------------------------------

    def _run_verify(
        self, script_dir: Path, plugin, config: dict
    ) -> list[str]:
        verify = getattr(plugin, "verify_script", None)
        if verify is None or getattr(plugin, "verify_location", "master") != "agent":
            raise ScriptPreflightError(
                f"插件未声明台架侧验证能力: task_type={getattr(plugin, 'task_type', '?')}",
                code="PLUGIN_NOT_FOUND",
            )
        return list(verify(str(script_dir), config))

    def _run_parse(
        self, script_dir: Path, plugin, config: dict
    ) -> list[dict]:
        parse = getattr(plugin, "parse_cases", None)
        if parse is None or getattr(plugin, "parse_location", "master") != "agent":
            raise ScriptPreflightError(
                f"插件未声明台架侧解析能力: task_type={getattr(plugin, 'task_type', '?')}",
                code="PLUGIN_NOT_FOUND",
            )
        cases = parse(str(script_dir), config)
        return [self._case_to_dict(case) for case in cases]

    @staticmethod
    def _case_to_dict(case) -> dict:
        """CaseInfo/兼容对象 → 事实 dict（stable_key/name/parent_path/tags/params）。"""
        return {
            "stable_key": getattr(case, "stable_key", ""),
            "name": getattr(case, "name", ""),
            "parent_path": getattr(case, "parent_path", ""),
            "tags": list(getattr(case, "tags", ())),
            "params": dict(getattr(case, "params", {})),
        }

    # -- 结果回传 -----------------------------------------------------------

    def _enqueue_verify_result(
        self, envelope: Envelope, script_id: str, *, errors: list[str]
    ) -> None:
        payload = ScriptVerifyResultPayload(
            verify_id=envelope.payload.get("verify_id", ""),
            script_id=script_id,
            project_id=envelope.payload.get("project_id", ""),
            errors=list(errors),
        )
        self._enqueue_event(
            envelope,
            MessageType.SCRIPT_VERIFY_RESULT,
            "verify-result",
            f"verify-result:{envelope.payload.get('verify_id', '')}",
            payload.model_dump(mode="json"),
        )

    def _enqueue_parse_result(
        self, envelope: Envelope, script_id: str, *, cases: list[dict]
    ) -> None:
        payload = ScriptParseResultPayload(
            parse_id=envelope.payload.get("parse_id", ""),
            script_id=script_id,
            cases=list(cases),
        )
        self._enqueue_event(
            envelope,
            MessageType.SCRIPT_PARSE_RESULT,
            "parse-result",
            f"parse-result:{envelope.payload.get('parse_id', '')}",
            payload.model_dump(mode="json"),
        )

    def _enqueue_error_result(
        self, envelope: Envelope, script_id: str, *, errors: list[str]
    ) -> None:
        """把预检失败以对应结果类型回传（verify/parse 二选一）。"""
        if envelope.message_type == MessageType.SCRIPT_VERIFY.value:
            self._enqueue_verify_result(envelope, script_id, errors=errors)
        else:
            payload = ScriptParseResultPayload(
                parse_id=envelope.payload.get("parse_id", ""),
                script_id=script_id,
                errors=list(errors),
            )
            self._enqueue_event(
                envelope,
                MessageType.SCRIPT_PARSE_RESULT,
                "parse-result",
                f"parse-result:{envelope.payload.get('parse_id', '')}",
                payload.model_dump(mode="json"),
            )

    def _enqueue_event(
        self,
        envelope: Envelope,
        message_type: MessageType,
        segment: str,
        outbox_id: str,
        payload: dict,
    ) -> None:
        result_envelope = Envelope(
            message_id=uuid.uuid4().hex,
            message_type=message_type.value,
            sent_at=self._now(),
            sender=Sender(
                kind=SenderKind.AGENT,
                id=self._settings.node_id,
                session_id=self._session_id(),
            ),
            correlation_id=envelope.message_id,
            trace_id=self._settings.node_id,
            payload=payload,
        )
        topic = event_topic(self._settings.node_id, segment)
        self._ledger.replace_outbox(
            outbox_id,
            topic,
            result_envelope.model_dump(mode="json"),
        )
        logger.info(
            "脚本预检结果已入 outbox: type=%s outbox_id=%s",
            message_type.value,
            outbox_id,
        )
