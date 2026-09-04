"""Agent 远程运维命令控制器。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import TypeAlias

from aetp_protocol.capabilities import AgentMaintenanceState
from aetp_protocol.envelope import Envelope, parse_message
from aetp_protocol.errors import ErrorCode
from aetp_protocol.ids import BusinessId, SessionId, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    LogLevelUpdateRequest,
    LogLevelUpdateResult,
    MaintenanceDrainRequest,
    MaintenanceDrainResult,
    MaintenanceRestartRequest,
    MaintenanceRestartResult,
)
from aetp_protocol.topics import (
    command_topic,
    parse_topic,
    validate_message_type_for_topic,
    validate_sender_for_topic,
)

from agent.application.services.agent_log_facade import AgentLogFacade
from agent.application.services.capability_publisher import CapabilityPublisher
from agent.domain.ledger import Ledger
from common.transport import MqttMessage

MaintenanceResult: TypeAlias = LogLevelUpdateResult | MaintenanceDrainResult | MaintenanceRestartResult
MaintenanceRequest: TypeAlias = LogLevelUpdateRequest | MaintenanceDrainRequest | MaintenanceRestartRequest

logger = logging.getLogger(__name__)


class AgentMaintenanceController:
    """校验并执行 Master 下发的日志级别、排空和重启命令。"""

    def __init__(
        self,
        node_id: BusinessId,
        ledger: Ledger,
        publisher: CapabilityPublisher,
        log_facade: AgentLogFacade,
        *,
        active_attempt_count: Callable[[], int] | None = None,
        is_registered: Callable[[], bool] | None = None,
        master_id: str = "aetp-master",
        sleep: Callable[[float], Awaitable[None]] | None = None,
        restart: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        self._node_id = node_id
        self._ledger = ledger
        self._publisher = publisher
        self._log_facade = log_facade
        self._active_attempt_count = active_attempt_count or (lambda: len(ledger.list_active_runs()))
        self._is_registered = is_registered or (lambda: True)
        self._master_id = master_id
        self._sleep = sleep or asyncio.sleep
        self._restart = restart or restart_process
        self._results: dict[str, MaintenanceResult] = {}

    def log_level_topic(self) -> str:
        return self._topic("agent.log.level.update")

    def drain_topic(self) -> str:
        return self._topic("agent.maintenance.drain")

    def restart_topic(self) -> str:
        return self._topic("agent.maintenance.restart")

    async def handle(self, message: MqttMessage, session_id: SessionId) -> bool:
        """处理一条维护命令；合法命令即使业务拒绝也返回 True。"""
        parsed = self._parse(message)
        if parsed is None:
            return False
        envelope, request = parsed
        if not self._is_registered() or request.expected_session_id != session_id:
            logger.warning(
                "维护命令被拒绝（未注册或 session 不匹配）: type=%s op=%s session=%s",
                type(request).__name__,
                request.operation_id.root,
                session_id.root,
            )
            return False
        logger.info(
            "收到维护命令: type=%s op=%s drain_timeout_s=%s",
            type(request).__name__,
            request.operation_id.root,
            getattr(request, "drain_timeout_s", None),
        )

        cached = self._results.get(request.operation_id.root)
        if cached is not None:
            await self._publish_result(cached, session_id, envelope.message_id)
            return True
        first_delivery = self._ledger.record_inbox(
            envelope.sender.id.root,
            envelope.message_id.root,
            envelope.message_type,
        )
        if not first_delivery:
            cached = self._results.get(request.operation_id.root)
            if cached is not None:
                await self._publish_result(cached, session_id, envelope.message_id)
                return True

        if isinstance(request, LogLevelUpdateRequest):
            result = self._apply_log_level(request)
        elif isinstance(request, MaintenanceDrainRequest):
            result = await self._drain(request, session_id, envelope.message_id, restart=False)
        else:
            result = await self._drain(request, session_id, envelope.message_id, restart=True)
        self._results[request.operation_id.root] = result
        await self._publish_result(result, session_id, envelope.message_id)
        if (
            isinstance(request, MaintenanceRestartRequest)
            and isinstance(result, MaintenanceRestartResult)
            and result.accepted
        ):
            logger.info("重启命令已接受，准备触发优雅重启: op=%s", request.operation_id.root)
            await self._restart_after_ack(request, session_id, envelope.message_id)
        return True

    def _parse(self, message: MqttMessage) -> tuple[Envelope, MaintenanceRequest] | None:
        try:
            topic = parse_topic(message.topic)
            if topic.node_id != self._node_id.root or topic.direction != "commands":
                return None
            envelope, payload = parse_message(json.loads(message.payload.decode("utf-8")))
            validate_sender_for_topic(message.topic, envelope.sender)
            validate_message_type_for_topic(
                message.topic,
                MessageType(envelope.message_type),
            )
            if envelope.sender.id != stable_id(self._master_id):
                return None
            if envelope.message_type == MessageType.AGENT_LOG_LEVEL_UPDATE.value and isinstance(
                payload, LogLevelUpdateRequest
            ):
                return envelope, payload
            if envelope.message_type == MessageType.AGENT_MAINTENANCE_DRAIN.value and isinstance(
                payload, MaintenanceDrainRequest
            ):
                return envelope, payload
            if envelope.message_type == MessageType.AGENT_MAINTENANCE_RESTART.value and isinstance(
                payload, MaintenanceRestartRequest
            ):
                return envelope, payload
            return None
        except Exception:
            return None

    def _apply_log_level(self, request: LogLevelUpdateRequest) -> LogLevelUpdateResult:
        try:
            self._log_facade.update_level(
                request.component,
                request.level,
                plugin_id=request.plugin_id,
                expires_at=request.expires_at,
            )
            logger.info(
                "日志级别已更新: op=%s component=%s level=%s",
                request.operation_id.root,
                request.component,
                request.level,
            )
            return LogLevelUpdateResult(
                node_id=self._node_id,
                operation_id=request.operation_id,
                accepted=True,
                level=request.level,
                message="日志级别已更新",
            )
        except Exception as exc:  # noqa: BLE001 - 运维边界返回结构化失败
            logger.warning(
                "日志级别更新失败: op=%s component=%s level=%s err=%s",
                request.operation_id.root,
                request.component,
                request.level,
                exc,
            )
            return LogLevelUpdateResult(
                node_id=self._node_id,
                operation_id=request.operation_id,
                accepted=False,
                code=ErrorCode("AGENT_MAINTENANCE"),
                message=f"日志级别更新失败: {exc}",
            )

    async def _drain(
        self,
        request: MaintenanceDrainRequest | MaintenanceRestartRequest,
        session_id: SessionId,
        correlation_id,
        *,
        restart: bool,
    ) -> MaintenanceDrainResult | MaintenanceRestartResult:
        initial_active = self._active_attempt_count()
        logger.info(
            "开始排空（restart=%s）: op=%s drain_timeout_s=%s 活动执行=%s",
            restart,
            request.operation_id.root,
            request.drain_timeout_s,
            initial_active,
        )
        await self._publisher.publish_maintenance_status(
            AgentMaintenanceState.DRAINING,
            session_id,
            active_attempt_count=initial_active,
            message="等待活动执行结束",
            correlation_id=correlation_id,
        )
        active_count = await self._wait_for_idle(request.drain_timeout_s)
        accepted = active_count == 0
        logger.info(
            "排空结束（restart=%s）: op=%s accepted=%s 剩余活动执行=%s",
            restart,
            request.operation_id.root,
            accepted,
            active_count,
        )
        if isinstance(request, MaintenanceDrainRequest) or not restart:
            result: MaintenanceDrainResult | MaintenanceRestartResult = MaintenanceDrainResult(
                node_id=self._node_id,
                operation_id=request.operation_id,
                accepted=accepted,
                active_attempt_count=active_count,
                code=None if accepted else ErrorCode("AGENT_MAINTENANCE"),
                message="Agent 已空闲" if accepted else f"排空超时，仍有 {active_count} 个活动执行",
            )
        else:
            result = MaintenanceRestartResult(
                node_id=self._node_id,
                operation_id=request.operation_id,
                accepted=accepted,
                code=None if accepted else ErrorCode("AGENT_MAINTENANCE"),
                message="Agent 已排空，准备重启" if accepted else f"排空超时，仍有 {active_count} 个活动执行",
            )
        if not accepted:
            await self._publisher.publish_maintenance_status(
                AgentMaintenanceState.IDLE,
                session_id,
                active_attempt_count=active_count,
                message="排空未完成",
                correlation_id=correlation_id,
            )
        elif isinstance(result, MaintenanceDrainResult):
            await self._publisher.publish_maintenance_status(
                AgentMaintenanceState.IDLE,
                session_id,
                active_attempt_count=0,
                message="排空完成",
                correlation_id=correlation_id,
            )
        else:
            await self._publisher.publish_maintenance_status(
                AgentMaintenanceState.RESTARTING,
                session_id,
                active_attempt_count=0,
                message="Agent 正在重启",
                correlation_id=correlation_id,
            )
        return result

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

    async def _restart_after_ack(
        self,
        request: MaintenanceRestartRequest,
        session_id: SessionId,
        correlation_id,
    ) -> None:
        try:
            logger.info("触发优雅重启回调: op=%s", request.operation_id.root)
            await self._restart_if_needed()
            logger.info("优雅重启已请求，等待主入口收尾 execv: op=%s", request.operation_id.root)
        except Exception as exc:  # noqa: BLE001 - 自重启失败只能进入降级状态
            logger.exception("Agent 自重启回调失败: op=%s", request.operation_id.root)
            await self._publisher.publish_maintenance_status(
                AgentMaintenanceState.DEGRADED,
                session_id,
                active_attempt_count=self._active_attempt_count(),
                message=f"Agent 自重启失败: {exc}",
                correlation_id=correlation_id,
            )

    async def _restart_if_needed(self) -> None:
        result = self._restart()
        if result is not None:
            await result

    async def _publish_result(
        self,
        result: MaintenanceResult,
        session_id: SessionId,
        correlation_id,
    ) -> None:
        if isinstance(result, LogLevelUpdateResult):
            await self._publisher.publish_log_level_result(
                result,
                session_id,
                correlation_id=correlation_id,
            )
        elif isinstance(result, MaintenanceDrainResult):
            await self._publisher.publish_maintenance_drain_result(
                result,
                session_id,
                correlation_id=correlation_id,
            )
        else:
            await self._publisher.publish_maintenance_restart_result(
                result,
                session_id,
                correlation_id=correlation_id,
            )

    def _topic(self, segment: str) -> str:
        return command_topic(self._node_id.root, segment)


def restart_process() -> None:
    """用当前解释器替换 Agent 进程，启动参数由部署器负责提供。"""
    if getattr(sys, "frozen", False):
        os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
    else:
        os.execv(sys.executable, [sys.executable, "-m", "agent.main", *sys.argv[1:]])


__all__ = ["AgentMaintenanceController", "restart_process"]
