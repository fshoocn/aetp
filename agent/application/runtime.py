"""Agent 运行时组合编排（P5.3，后续 P5.4 的接入点）。

``AgentRuntime`` 是 Agent 的唯一生命周期协调者：

1. 注册消息处理器和 Transport 连接回调；
2. 订阅当前节点的 register-ack 主题；
3. 启动 Transport 与本地 outbox publisher；
4. 连接成功后由 RegistrationService 写入注册 outbox；
5. ACK 校验成功后启动心跳；断开时停止心跳并等待重连重新注册；
6. 关闭时按 heartbeat -> outbox -> transport 的顺序释放资源。

P5.4 会在 ``_handle_message`` 中增加 run.assign/cancel 路由；本模块保留
该边界，避免把启动逻辑散落在 Transport 或业务服务中。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from aetp_protocol.envelope import Envelope
from aetp_protocol.execution import ExecutionPlan
from aetp_protocol.message_types import MessageType
from aetp_protocol.topics import (
    command_topic,
    v2_command_topic,
    validate_message_type_for_topic,
    validate_sender_for_topic,
)

from agent.application.services.artifact_upload_service import ArtifactUploadService
from agent.application.services.command_dispatcher import CommandDispatcher
from agent.application.services.execution_service import ExecutionService
from agent.application.services.registration_service import (
    RegistrationRejectedError,
    RegistrationService,
    RegistrationTimeoutError,
)
from agent.application.services.resource_provider import ResourceProviderRegistry
from agent.application.services.run_orchestrator import RunOrchestrator
from agent.application.services.script_cache_service import ScriptCacheService
from agent.application.services.script_preflight_service import (
    ScriptPreflightService,
)
from agent.application.services.v2_capability_publisher import AgentV2CapabilityPublisher
from agent.application.services.v2_execution_plan_controller import AgentV2ExecutionPlanController
from agent.application.services.v2_execution_runner import V2ExecutionRunner
from agent.application.services.v2_lease_renewal_service import AgentV2LeaseRenewalService
from agent.application.services.v2_plugin_sync_controller import AgentV2PluginSyncController
from agent.application.services.v2_reconcile_service import AgentV2ReconcileService
from agent.config import AgentSettings
from agent.domain.enums import AgentOutboxStatus
from agent.domain.ledger import Ledger
from common.backoff import ExponentialBackoff
from common.transport import MqttMessage, Transport

if TYPE_CHECKING:
    from agent.plugins import AgentPluginRegistry
    from agent.plugins.installer import PluginPackageInstaller
    from agent.plugins.v2_installer import V2PluginInstaller
    from agent.plugins.v2_registry import AgentV2PluginRegistry

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Agent 配置、账本、Transport、注册与心跳的生命周期编排。"""

    def __init__(
        self,
        settings: AgentSettings,
        transport: Transport,
        ledger: Ledger,
        registration: RegistrationService,
        dispatcher: CommandDispatcher | None = None,
        *,
        plugin_registry: AgentPluginRegistry | None = None,
        plugin_installer: PluginPackageInstaller | None = None,
        script_cache: ScriptCacheService | None = None,
        artifact_uploader: ArtifactUploadService | None = None,
        script_preflight: ScriptPreflightService | None = None,
        execution_service: ExecutionService | None = None,
        capability_cache=None,
        v2_capability_publisher: AgentV2CapabilityPublisher | None = None,
        v2_plugin_installer: V2PluginInstaller | None = None,
        v2_plugin_registry: AgentV2PluginRegistry | None = None,
        v2_plugin_sync_controller: AgentV2PluginSyncController | None = None,
        v2_execution_plan_controller: AgentV2ExecutionPlanController | None = None,
        v2_execution_runner: V2ExecutionRunner | None = None,
        v2_executor_resolver: Callable[[ExecutionPlan], object] | None = None,
        v2_lease_renewal_service: AgentV2LeaseRenewalService | None = None,
        v2_reconcile_service: AgentV2ReconcileService | None = None,
        resource_providers: ResourceProviderRegistry | None = None,
        sleep: Callable[[float], asyncio.Future] | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._ledger = ledger
        self._registration = registration
        self._capability_cache = capability_cache
        self._v2_capability_publisher = v2_capability_publisher
        self._v2_plugin_installer = v2_plugin_installer
        self._v2_plugin_registry = v2_plugin_registry
        self._v2_plugin_sync_controller = v2_plugin_sync_controller
        self._v2_execution_plan_controller = v2_execution_plan_controller
        self._v2_execution_runner = v2_execution_runner
        self._v2_executor_resolver = v2_executor_resolver
        self._v2_lease_renewal_service = v2_lease_renewal_service
        self._v2_reconcile_service = v2_reconcile_service
        self._resource_providers = resource_providers
        self._sleep = sleep or asyncio.sleep
        self._outbox_task: asyncio.Task[None] | None = None
        self._registration_task: asyncio.Task[None] | None = None
        self._lease_renewal_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._outbox_backoff = ExponentialBackoff()

        # 子组件组装（延迟构建，避免构造函数过重）
        self._execution_service = execution_service
        self._plugin_registry = plugin_registry
        self._plugin_installer = plugin_installer
        self._script_cache = script_cache
        self._artifact_uploader = artifact_uploader
        self._orchestrator: RunOrchestrator | None = None
        self._dispatcher_obj: CommandDispatcher | None = dispatcher
        self._script_preflight_obj: ScriptPreflightService | None = script_preflight

    def _ensure_components(self) -> None:
        """延迟构建子组件（首次使用时调用）。"""
        if self._execution_service is None:
            self._execution_service = ExecutionService(settings=self._settings, ledger=self._ledger)
        if self._orchestrator is None:
            self._orchestrator = RunOrchestrator(
                settings=self._settings,
                ledger=self._ledger,
                execution_service=self._execution_service,
                plugin_registry=self._plugin_registry,
                script_cache=self._script_cache,
                artifact_uploader=self._artifact_uploader,
                session_id=lambda: self._registration.session_id,
            )
        if self._dispatcher_obj is None:
            self._dispatcher_obj = CommandDispatcher(
                settings=self._settings,
                ledger=self._ledger,
                is_registered=lambda: self._registration.registered,
                plugin_registry=self._plugin_registry,
                plugin_installer=self._plugin_installer,
                script_cache=self._script_cache,
                execution_service=self._execution_service,
                orchestrator=self._orchestrator,
                session_id=lambda: self._registration.session_id,
            )
        if self._script_preflight_obj is None and self._script_cache is not None:
            self._script_preflight_obj = ScriptPreflightService(
                settings=self._settings,
                ledger=self._ledger,
                script_cache=self._script_cache,
                plugin_registry=self._plugin_registry,
                is_registered=lambda: self._registration.registered,
                session_id=lambda: self._registration.session_id,
            )
        if (
            self._v2_plugin_sync_controller is None
            and self._v2_capability_publisher is not None
            and self._v2_plugin_installer is not None
            and self._v2_plugin_registry is not None
        ):
            from aetp_protocol.ids import BusinessId

            self._v2_plugin_sync_controller = AgentV2PluginSyncController(
                BusinessId(self._settings.node_id),
                self._ledger,
                self._v2_plugin_installer,
                self._v2_plugin_registry,
                self._v2_capability_publisher,
                master_id=self._settings.master_id,
            )
        if self._v2_lease_renewal_service is None and self._v2_capability_publisher is not None:
            from aetp_protocol.ids import BusinessId

            self._v2_lease_renewal_service = AgentV2LeaseRenewalService(
                BusinessId(self._settings.node_id),
                self._ledger,
                self._v2_capability_publisher,
                master_id=self._settings.master_id,
            )
        if self._v2_reconcile_service is None and self._v2_capability_publisher is not None:
            from aetp_protocol.ids import BusinessId

            self._v2_reconcile_service = AgentV2ReconcileService(
                BusinessId(self._settings.node_id),
                self._ledger,
                self._v2_capability_publisher,
                master_id=self._settings.master_id,
            )
        if (
            self._v2_execution_runner is None
            and self._v2_capability_publisher is not None
            and self._v2_executor_resolver is not None
        ):
            assert self._execution_service is not None
            self._v2_execution_runner = V2ExecutionRunner(
                self._settings,
                self._ledger,
                self._execution_service,
                self._v2_capability_publisher,
                self._v2_executor_resolver,
                script_cache=self._script_cache,
                resource_providers=self._resource_providers,
                artifact_uploader=self._artifact_uploader,
            )
        if (
            self._v2_execution_plan_controller is None
            and self._v2_capability_publisher is not None
            and self._v2_plugin_registry is not None
        ):
            from aetp_protocol.ids import BusinessId

            publisher = self._v2_capability_publisher
            self._v2_execution_plan_controller = AgentV2ExecutionPlanController(
                BusinessId(self._settings.node_id),
                self._ledger,
                publisher,
                self._v2_plugin_registry,
                script_cache=self._script_cache,
                is_registered=lambda: publisher.v2_registered,
                master_id=self._settings.master_id,
                lease_renewal=self._v2_lease_renewal_service,
                execution_runner=self._v2_execution_runner,
                execution_service=self._execution_service,
            )
        assert self._dispatcher_obj is not None
        assert self._orchestrator is not None

    async def start(self) -> None:
        """启动 Agent 主生命周期（幂等）。"""
        if self._outbox_task is not None:
            return
        self._ensure_components()
        self._stop_event.clear()
        self._transport.on_message(self._handle_message)
        self._transport.on_connection_change(self._handle_connection_change)
        subscriptions = [
            command_topic(self._settings.node_id, "register-ack"),
            command_topic(self._settings.node_id, "assign"),
            command_topic(self._settings.node_id, "cancel"),
            command_topic(self._settings.node_id, "verify"),
            command_topic(self._settings.node_id, "parse"),
        ]
        if self._v2_capability_publisher is not None:
            subscriptions.extend(
                [
                    self._v2_capability_publisher.register_ack_topic(),
                    self._v2_capability_publisher.diagnostics_command_topic(),
                ]
            )
        if self._v2_plugin_sync_controller is not None:
            subscriptions.append(self._v2_plugin_sync_controller.command_topic())
        if self._v2_execution_plan_controller is not None:
            subscriptions.append(self._v2_execution_plan_controller.command_topic())
            subscriptions.append(self._v2_execution_plan_controller.cancel_command_topic())
        if self._v2_lease_renewal_service is not None:
            subscriptions.append(v2_command_topic(self._settings.node_id, "lease.renewed"))
        if self._v2_reconcile_service is not None:
            subscriptions.append(v2_command_topic(self._settings.node_id, "execution.reconcile_result"))
        await self._transport.subscribe(subscriptions)
        self._outbox_task = asyncio.create_task(self._outbox_loop())
        if self._v2_lease_renewal_service is not None:
            self._lease_renewal_task = asyncio.create_task(self._lease_renewal_loop())
        await self._transport.connect()
        # 启动 USB 插拔监听（可选，usb-monitor 未安装/失败时静默降级为指纹兜底）
        if self._capability_cache is not None:
            try:
                self._capability_cache.start_usb_monitoring()
            except Exception:
                logger.debug("启动 USB 插拔监听失败（已忽略）", exc_info=True)
        logger.info("Agent runtime 已启动: node=%s", self._settings.node_id)

    async def stop(self) -> None:
        """停止 Agent 主生命周期（幂等，每步容错不阻塞后续清理）。"""
        self._stop_event.set()
        try:
            await self._registration.stop_heartbeat()
        except Exception:
            logger.debug("停止心跳异常（已忽略）", exc_info=True)
        if self._capability_cache is not None:
            try:
                self._capability_cache.stop_usb_monitoring()
            except Exception:
                logger.debug("停止 USB 插拔监听异常（已忽略）", exc_info=True)
        self._cancel_registration_waiter()
        if self._lease_renewal_task is not None:
            self._lease_renewal_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._lease_renewal_task
            self._lease_renewal_task = None
        if self._v2_execution_runner is not None:
            await self._v2_execution_runner.stop()
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("停止 outbox 异常（已忽略）", exc_info=True)
            self._outbox_task = None
        try:
            await self._transport.disconnect()
        except Exception:
            logger.debug("断开 MQTT 异常（已忽略）", exc_info=True)
        logger.info("Agent runtime 已停止: node=%s", self._settings.node_id)

    async def _handle_connection_change(self, connected: bool, session_id: str | None = None) -> None:
        """连接成功触发重新注册，断开撤销注册状态。"""
        await self._registration.handle_connection_change(connected, session_id)
        if connected:
            if self._v2_capability_publisher is not None:
                try:
                    if self._v2_plugin_sync_controller is not None:
                        self._v2_plugin_sync_controller.reset_session()
                    self._v2_capability_publisher.reset_session()
                    self._v2_capability_publisher.enqueue_register(
                        self._ledger,
                        self._v2_session_id(),
                    )
                except Exception:
                    logger.exception("V2 节点注册写入 outbox 失败")
            self._cancel_registration_waiter()
            self._registration_task = asyncio.create_task(self._wait_for_registration())
        else:
            self._cancel_registration_waiter()
            if self._v2_capability_publisher is not None:
                self._v2_capability_publisher.reset_session()
            if self._v2_plugin_sync_controller is not None:
                self._v2_plugin_sync_controller.reset_session()
            if self._v2_lease_renewal_service is not None:
                self._v2_lease_renewal_service.reset_session()
            if self._v2_reconcile_service is not None:
                self._v2_reconcile_service.reset_session()

    async def _wait_for_registration(self) -> None:
        """等待 ACK；超时后按指数退避重发注册（保持 broker 连接）。

        关键：register-ack 超时通常意味着 Master 不在线或尚未订阅，
        而 Agent 与 broker 的连接是健康的。此时不应断开 broker 连接
        再重连，而应保持连接、按退避间隔重发注册消息并继续等待 ACK，
        避免 Master 长时间离线时以固定频率高频重发（惊群/浪费带宽）。
        """
        registration_backoff = ExponentialBackoff(
            base_delay_s=max(1.0, float(self._settings.registration_timeout_s)),
            max_delay_s=60.0,
        )
        while not self._stop_event.is_set():
            try:
                await self._registration.wait_for_register_ack()
                await self._registration.start_heartbeat()
                if self._v2_capability_publisher is not None:
                    try:
                        await self._v2_capability_publisher.publish_snapshot(
                            self._v2_session_id()
                        )
                    except Exception:
                        logger.exception("V2 能力快照发布失败")
                if self._v2_reconcile_service is not None:
                    try:
                        self._v2_reconcile_service.enqueue(self._v2_session_id())
                    except Exception:
                        logger.exception("V2 execution.reconcile 写入 outbox 失败")
                return
            except asyncio.CancelledError:
                raise
            except RegistrationTimeoutError as exc:
                # Master 未回 ACK：保持连接，按退避重发注册，继续等待
                delay = registration_backoff.next()
                logger.warning("Agent 注册超时，%.1fs 后重发注册: %s", delay, exc)
                await self._sleep(delay)
                if self._stop_event.is_set():
                    return
                self._registration.enqueue_register()
                if self._v2_capability_publisher is not None:
                    try:
                        self._v2_capability_publisher.enqueue_register(
                            self._ledger,
                            self._v2_session_id(),
                        )
                    except Exception:
                        logger.exception("V2 节点注册重发写入 outbox 失败")
            except RegistrationRejectedError as exc:
                # 被拒绝：通常需重新连接（如 session 冲突），断开重连
                logger.warning("Agent 注册被拒绝，将触发重连: %s", exc)
                await self._transport.disconnect()
                if not self._stop_event.is_set():
                    await self._transport.connect()
                return
            except Exception:
                logger.exception("Agent 注册等待异常")
                await self._transport.disconnect()
                if not self._stop_event.is_set():
                    await self._transport.connect()
                return

    def _cancel_registration_waiter(self) -> None:
        """取消旧 session 的 ACK 等待任务。"""
        if self._registration_task is not None:
            self._registration_task.cancel()
            self._registration_task = None

    async def _handle_message(self, message: MqttMessage) -> None:
        """路由入站消息到 register-ack / 命令分发器 / 脚本预检（P5.4/P5.7）。"""
        assert self._dispatcher_obj is not None, "组件未初始化，请先调用 start()"
        if self._registration.handle_register_ack(message):
            return
        if self._v2_capability_publisher is not None and self._v2_capability_publisher.handle_register_ack(
            message,
            self._v2_session_id(),
        ):
            return
        if (
            self._v2_plugin_sync_controller is not None
            and message.topic == self._v2_plugin_sync_controller.command_topic()
        ):
            await self._v2_plugin_sync_controller.handle(message, self._v2_session_id())
            return
        if (
            self._v2_execution_plan_controller is not None
            and message.topic == self._v2_execution_plan_controller.command_topic()
        ):
            await self._v2_execution_plan_controller.handle(message, self._v2_session_id())
            return
        if (
            self._v2_execution_plan_controller is not None
            and message.topic == self._v2_execution_plan_controller.cancel_command_topic()
        ):
            await self._v2_execution_plan_controller.handle_cancel(message, self._v2_session_id())
            return
        if (
            self._v2_lease_renewal_service is not None
            and message.topic == v2_command_topic(self._settings.node_id, "lease.renewed")
        ):
            self._v2_lease_renewal_service.handle_renewed(message, self._v2_session_id())
            return
        if (
            self._v2_reconcile_service is not None
            and message.topic == v2_command_topic(self._settings.node_id, "execution.reconcile_result")
        ):
            self._v2_reconcile_service.handle_result(message, self._v2_session_id())
            return
        if (
            self._v2_capability_publisher is not None
            and message.topic == self._v2_capability_publisher.diagnostics_command_topic()
        ):
            await self._v2_capability_publisher.handle_diagnostics_request(
                message,
                self._v2_session_id(),
            )
            return
        # P5.7：script.verify / script.parse 命令路由到脚本预检服务
        if self._route_script_preflight(message):
            return
        # P5.4：run.assign / run.cancel 命令路由
        self._dispatcher_obj.handle_command(message)

    def _v2_session_id(self):
        from aetp_protocol.ids import SessionId

        return SessionId(self._registration.session_id)

    async def _lease_renewal_loop(self) -> None:
        """周期性为已接受 V2 Plan 生成 Lease 续租请求。"""
        while not self._stop_event.is_set():
            if (
                self._v2_lease_renewal_service is not None
                and self._v2_capability_publisher is not None
                and self._v2_capability_publisher.v2_registered
            ):
                try:
                    await self._v2_lease_renewal_service.run_once(self._v2_session_id())
                except Exception:
                    logger.exception("V2 Lease 续租轮询失败")
            await self._sleep(max(1.0, min(float(self._settings.heartbeat_interval_s), 5.0)))

    def _route_script_preflight(self, message: MqttMessage) -> bool:
        """解析入站命令；若为 script.verify/script.parse 则交给预检服务。"""
        if self._script_preflight_obj is None:
            return False
        try:
            envelope = Envelope.model_validate(json.loads(message.payload.decode("utf-8")))
            validate_sender_for_topic(message.topic, envelope.sender)
            validate_message_type_for_topic(message.topic, MessageType(envelope.message_type))
        except Exception:  # noqa: BLE001 - 非预检命令交由 dispatcher 处理
            return False
        msg_type = envelope.message_type
        if msg_type == MessageType.SCRIPT_VERIFY.value:
            self._script_preflight_obj.handle_verify(message.topic, envelope)
            return True
        if msg_type == MessageType.SCRIPT_PARSE.value:
            self._script_preflight_obj.handle_parse(message.topic, envelope)
            return True
        return False

    async def _outbox_loop(self) -> None:
        """本地 outbox publisher：发送成功标记 sent，失败按租约重试。"""
        while not self._stop_event.is_set():
            now = datetime.now(UTC).replace(tzinfo=None)
            entries = self._ledger.claim_due_outbox(100, now)
            for entry in entries:
                try:
                    await self._transport.publish(
                        entry.topic,
                        json.dumps(entry.payload).encode("utf-8"),
                        qos=1,
                    )
                except Exception as exc:  # noqa: BLE001 - 单条失败不阻塞批次
                    retry_at = now + timedelta(seconds=min(60, 2 ** min(entry.attempts + 1, 6)))
                    self._ledger.mark_outbox(
                        entry.outbox_id,
                        status=AgentOutboxStatus.PENDING,
                        attempts=entry.attempts + 1,
                        next_attempt_at=retry_at,
                    )
                    logger.warning(
                        "Agent outbox 发布失败: id=%s error=%s retry_at=%s",
                        entry.outbox_id,
                        exc,
                        retry_at,
                    )
                else:
                    self._ledger.mark_outbox(
                        entry.outbox_id,
                        status=AgentOutboxStatus.SENT,
                        attempts=entry.attempts + 1,
                        next_attempt_at=None,
                    )
                    self._outbox_backoff.reset()
            await self._sleep(0.1 if entries else 0.5)
