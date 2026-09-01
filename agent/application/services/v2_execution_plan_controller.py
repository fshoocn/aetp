"""Agent V2 execution.plan 预检和 ACK 控制器。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from aetp_protocol.errors import ErrorCode
from aetp_protocol.execution import ExecutionPlan
from aetp_protocol.ids import BusinessId, MessageId, SessionId, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import ExecutionAck
from aetp_protocol.plan_hash import calculate_plan_hash
from aetp_protocol.topics import (
    parse_v2_topic,
    v2_command_topic,
    validate_message_type_for_v2_topic,
    validate_sender_for_v2_topic,
)
from aetp_protocol.v2_envelope import V2Envelope, parse_v2_message

from agent.application.services.script_cache_service import ScriptCacheError, ScriptCacheService
from agent.application.services.v2_capability_publisher import AgentV2CapabilityPublisher
from agent.application.services.v2_lease_renewal_service import AgentV2LeaseRenewalService
from agent.domain.ledger import Ledger
from agent.plugins.v2_registry import AgentV2PluginRegistry
from common.transport import MqttMessage

_PLAN_INVALID = ErrorCode("EXECUTION_PLAN_INVALID")
_PLUGIN_UNAVAILABLE = ErrorCode("PLUGIN_VERSION_UNAVAILABLE")
_SCRIPT_INVALID = ErrorCode("SCRIPT_CHECKSUM_MISMATCH")
_LEASE_EXPIRED = ErrorCode("RESOURCE_LEASE_EXPIRED")
_STALE_SESSION = ErrorCode("STALE_SESSION")
_STALE_ATTEMPT = ErrorCode("STALE_ATTEMPT")


class AgentV2ExecutionPlanController:
    """只接受通过全部固定引用校验的 V2 Plan，不在 M3 执行插件代码。"""

    def __init__(
        self,
        node_id: BusinessId,
        ledger: Ledger,
        publisher: AgentV2CapabilityPublisher,
        registry: AgentV2PluginRegistry,
        *,
        script_cache: ScriptCacheService | None = None,
        is_registered: Callable[[], bool] | None = None,
        master_id: str = "aetp-master",
        now: Callable[[], datetime] | None = None,
        lease_renewal: AgentV2LeaseRenewalService | None = None,
    ) -> None:
        self._node_id = node_id
        self._ledger = ledger
        self._publisher = publisher
        self._registry = registry
        self._script_cache = script_cache
        self._is_registered = is_registered or (lambda: True)
        self._master_id = master_id
        self._now = now or (lambda: datetime.now(UTC))
        self._lease_renewal = lease_renewal

    def command_topic(self) -> str:
        """返回本节点 execution.plan 命令主题。"""
        return v2_command_topic(self._node_id.root, "execution.plan")

    async def handle(self, message: MqttMessage, session_id: SessionId) -> bool:
        """预检并 ACK 一条 execution.plan；合法 V2 消息均视为已消费。"""
        parsed = self._parse_plan(message)
        if parsed is None:
            return False
        envelope, plan = parsed

        if not self._is_registered():
            await self._reject(plan, session_id, envelope.message_id, _STALE_SESSION, "V2 节点尚未注册")
            return True
        if plan.node_id != self._node_id or plan.target_session_id != session_id:
            await self._reject(plan, session_id, envelope.message_id, _STALE_SESSION, "Plan 目标 session 不匹配")
            return True

        existing = self._ledger.get_run(plan.run_id.root)
        if existing is not None and existing.plan_id not in (None, plan.plan_id.root):
            await self._reject(plan, session_id, envelope.message_id, _STALE_ATTEMPT, "Run 已绑定其它 Plan")
            return True

        failure = self._validate_plan(plan)
        if failure is not None:
            await self._reject(plan, session_id, envelope.message_id, *failure)
            return True

        if self._script_cache is not None:
            try:
                self._script_cache.ensure_cached(plan.script.model_dump(mode="json"))
            except ScriptCacheError as exc:
                await self._reject(
                    plan,
                    session_id,
                    envelope.message_id,
                    _SCRIPT_INVALID,
                    f"脚本准备失败: {exc}",
                )
                return True

        claimed = self._ledger.claim_run(
            plan.run_id.root,
            plan.attempt_no,
            [binding.resource_id.root for binding in plan.resource_bindings],
            plan.plan_id.root,
        )
        if not claimed:
            existing = self._ledger.get_run(plan.run_id.root)
            if existing is None or existing.plan_id != plan.plan_id.root:
                await self._reject(plan, session_id, envelope.message_id, _STALE_ATTEMPT, "Attempt 已被其它 Plan 占用")
                return True
        else:
            self._ledger.record_inbox(
                envelope.sender.id.root,
                envelope.message_id.root,
                envelope.message_type,
            )
        await self._accept(plan, session_id, envelope.message_id)
        if self._lease_renewal is not None:
            self._lease_renewal.register_plan(plan)
        return True

    def _parse_plan(self, message: MqttMessage) -> tuple[V2Envelope, ExecutionPlan] | None:
        try:
            topic = parse_v2_topic(message.topic)
            if (
                topic.direction != "commands"
                or topic.node_id != self._node_id.root
                or topic.segment != "execution.plan"
            ):
                return None
            envelope, payload = parse_v2_message(json.loads(message.payload.decode("utf-8")))
            validate_sender_for_v2_topic(message.topic, envelope.sender)
            validate_message_type_for_v2_topic(
                message.topic,
                MessageType(envelope.message_type),
            )
            if envelope.sender.id != stable_id(self._master_id):
                return None
            if envelope.message_type != MessageType.EXECUTION_PLAN.value or not isinstance(payload, ExecutionPlan):
                return None
            return envelope, payload
        except Exception:
            return None

    def _validate_plan(self, plan: ExecutionPlan) -> tuple[ErrorCode, str] | None:
        now = self._now()
        try:
            if calculate_plan_hash(plan) != plan.plan_hash:
                return _PLAN_INVALID, "Plan hash 校验失败"
        except Exception:
            return _PLAN_INVALID, "Plan hash 无法计算"
        if plan.deadline_at <= now:
            return _PLAN_INVALID, "Plan deadline 已过期"
        if any(
            binding.expires_at <= now or binding.expires_at > plan.deadline_at
            for binding in plan.resource_bindings
        ):
            return _LEASE_EXPIRED, "Plan 中存在已过期或超过 deadline 的 Lease"
        installed = self._registry.get(plan.executor.plugin_id.root, plan.executor.version.root)
        if installed is None:
            return _PLUGIN_UNAVAILABLE, "Agent 未安装 Plan 指定的插件版本"
        if (
            plan.plugin_package is not None
            and (
                plan.plugin_package.plugin_id != plan.executor.plugin_id
                or plan.plugin_package.version != plan.executor.version
                or plan.plugin_package.archive_sha256 != installed.ref.archive_sha256
            )
        ):
            return _PLUGIN_UNAVAILABLE, "Plan 插件归档与 Agent 本地版本不一致"
        if (
            self._script_cache is None
            and plan.script.download_url is None
            and self._ledger.get_cached_script(
                plan.script.script_id.root,
                plan.script.version,
                plan.script.sha256.root,
            )
            is None
        ):
            return _SCRIPT_INVALID, "Plan 脚本未提供下载地址且本地无缓存"
        return None

    async def _accept(self, plan: ExecutionPlan, session_id: SessionId, correlation_id: MessageId) -> None:
        self._publisher.enqueue_execution_ack(
            self._ledger,
            ExecutionAck(
                run_id=plan.run_id,
                shard_id=plan.shard_id,
                attempt_id=plan.attempt_id,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                accepted=True,
            ),
            session_id,
            correlation_id=correlation_id,
        )

    async def _reject(
        self,
        plan: ExecutionPlan,
        session_id: SessionId,
        correlation_id: MessageId,
        code: ErrorCode,
        message: str,
    ) -> None:
        self._publisher.enqueue_execution_ack(
            self._ledger,
            ExecutionAck(
                run_id=plan.run_id,
                shard_id=plan.shard_id,
                attempt_id=plan.attempt_id,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                accepted=False,
                code=code,
                message=message,
            ),
            session_id,
            correlation_id=correlation_id,
        )


__all__ = ["AgentV2ExecutionPlanController"]
