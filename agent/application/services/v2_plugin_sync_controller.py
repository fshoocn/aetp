"""Agent V2 插件同步消息控制器。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from aetp_protocol.capabilities import AgentMaintenanceState
from aetp_protocol.errors import ErrorCode
from aetp_protocol.ids import BusinessId, SessionId, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.plugins import PluginSyncItemResult, PluginSyncRequest, PluginSyncResult
from aetp_protocol.topics import (
    parse_v2_topic,
    v2_command_topic,
    validate_message_type_for_v2_topic,
    validate_sender_for_v2_topic,
)
from aetp_protocol.v2_envelope import V2Envelope, parse_v2_message

from agent.application.services.plugin_sync_service import (
    AgentPluginSyncService,
    V2PluginInstallPort,
)
from agent.application.services.v2_capability_publisher import AgentV2CapabilityPublisher
from agent.domain.ledger import Ledger
from agent.plugins.v2_registry import AgentV2PluginRegistry
from common.transport import MqttMessage


class AgentV2PluginSyncController:
    """校验并执行 Master 下发的 V2 插件同步命令。"""

    def __init__(
        self,
        node_id: BusinessId,
        ledger: Ledger,
        installer: V2PluginInstallPort,
        registry: AgentV2PluginRegistry,
        publisher: AgentV2CapabilityPublisher,
        *,
        active_attempt_count: Callable[[], int] | None = None,
        master_id: str = "aetp-master",
        restart: Callable[[], Awaitable[None] | None] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._node_id = node_id
        self._ledger = ledger
        self._installer = installer
        self._registry = registry
        self._publisher = publisher
        self._master_id = master_id
        self._restart = restart
        self._sleep = sleep or asyncio.sleep
        self._active_attempt_count = active_attempt_count or (lambda: len(ledger.list_active_runs()))
        self._results: dict[str, PluginSyncResult] = {}

    def command_topic(self) -> str:
        """返回本节点插件同步命令主题。"""
        return v2_command_topic(self._node_id.root, "agent.plugin.sync")

    def reset_session(self) -> None:
        """切换 session 时清除当前进程内的同步结果缓存。"""
        self._results.clear()

    async def handle(self, message: MqttMessage, session_id: SessionId) -> bool:
        """处理一条 V2 同步命令，成功消费返回 True。"""
        request_message = self._parse_request(message)
        if request_message is None:
            return False
        envelope, request = request_message
        if request.node_id != self._node_id or request.expected_session_id != session_id:
            return False

        cached = self._results.get(request.sync_id.root)
        if cached is not None:
            await self._publisher.publish_plugin_sync_result(
                cached,
                session_id,
                correlation_id=envelope.message_id,
            )
            return True
        if not self._ledger.record_inbox(
            envelope.sender.id.root,
            envelope.message_id.root,
            envelope.message_type,
        ):
            cached = self._results.get(request.sync_id.root)
            if cached is not None:
                await self._publisher.publish_plugin_sync_result(
                    cached,
                    session_id,
                    correlation_id=envelope.message_id,
                )
                return True

        await self._publisher.publish_maintenance_status(
            AgentMaintenanceState.DRAINING,
            session_id,
            sync_id=request.sync_id,
            active_attempt_count=self._active_attempt_count(),
            message="等待插件同步窗口",
            correlation_id=envelope.message_id,
        )
        active_count = await self._wait_for_idle(request.drain_timeout_s)
        if active_count:
            result = self._skipped_result(request, active_count)
        else:
            await self._publisher.publish_maintenance_status(
                AgentMaintenanceState.UPDATING,
                session_id,
                sync_id=request.sync_id,
                active_attempt_count=0,
                message="正在同步插件",
                correlation_id=envelope.message_id,
            )
            try:
                result = AgentPluginSyncService(
                    self._installer,
                    session_id,
                    self._registry,
                ).apply(request)
            except Exception as exc:  # noqa: BLE001 - 同步边界统一返回结构化失败
                result = self._failed_result(request, str(exc))

        self._results[request.sync_id.root] = result
        await self._publisher.publish_plugin_sync_result(
            result,
            session_id,
            correlation_id=envelope.message_id,
        )
        final_state = (
            AgentMaintenanceState.RESTARTING
            if result.accepted and result.restart_required
            else AgentMaintenanceState.IDLE
            if result.accepted
            else AgentMaintenanceState.DEGRADED
        )
        await self._publisher.publish_maintenance_status(
            final_state,
            session_id,
            sync_id=request.sync_id,
            active_attempt_count=self._active_attempt_count(),
            message=(
                "插件同步完成，准备重启"
                if result.accepted and result.restart_required
                else "插件同步完成"
                if result.accepted
                else "插件同步失败"
            ),
            correlation_id=envelope.message_id,
        )
        await self._publisher.publish_snapshot(session_id)
        restart = self._restart
        if result.accepted and result.restart_required and restart is not None:
            try:
                await self._restart_if_needed(restart)
            except Exception as exc:  # noqa: BLE001 - 自重启失败转为降级状态
                await self._publisher.publish_maintenance_status(
                    AgentMaintenanceState.DEGRADED,
                    session_id,
                    sync_id=request.sync_id,
                    active_attempt_count=self._active_attempt_count(),
                    message=f"Agent 自重启失败: {exc}",
                    correlation_id=envelope.message_id,
                )
        return True

    async def _wait_for_idle(self, timeout_s: int) -> int:
        active_count = self._active_attempt_count()
        if active_count == 0 or timeout_s == 0:
            return active_count
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while active_count:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await self._sleep(min(0.5, remaining))
            active_count = self._active_attempt_count()
        return active_count

    async def _restart_if_needed(self, restart: Callable[[], Awaitable[None] | None]) -> None:
        result = restart()
        if result is not None:
            await result

    def _parse_request(self, message: MqttMessage) -> tuple[V2Envelope, PluginSyncRequest] | None:
        try:
            topic = parse_v2_topic(message.topic)
            if (
                topic.direction != "commands"
                or topic.node_id != self._node_id.root
                or topic.segment != "agent.plugin.sync"
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
            if envelope.message_type != MessageType.AGENT_PLUGIN_SYNC.value:
                return None
            if not isinstance(payload, PluginSyncRequest):
                return None
            return envelope, payload
        except Exception:
            return None

    @staticmethod
    def _skipped_result(request: PluginSyncRequest, active_count: int) -> PluginSyncResult:
        return PluginSyncResult(
            sync_id=request.sync_id,
            node_id=request.node_id,
            accepted=False,
            restart_required=False,
            items=tuple(
                PluginSyncItemResult(
                    plugin_id=item.plugin_id,
                    version=item.version,
                    state="skipped",
                    unavailable_reasons=(ErrorCode("AGENT_MAINTENANCE"),),
                    message=f"Agent 有 {active_count} 个活动执行",
                )
                for item in request.items
            ),
        )

    @staticmethod
    def _failed_result(request: PluginSyncRequest, message: str) -> PluginSyncResult:
        return PluginSyncResult(
            sync_id=request.sync_id,
            node_id=request.node_id,
            accepted=False,
            restart_required=False,
            items=tuple(
                PluginSyncItemResult(
                    plugin_id=item.plugin_id,
                    version=item.version,
                    state="failed",
                    unavailable_reasons=(ErrorCode("PLUGIN_SYNC_FAILED"),),
                    message=message,
                )
                for item in request.items
            ),
        )


__all__ = ["AgentV2PluginSyncController"]
