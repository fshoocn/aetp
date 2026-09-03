"""Agent 运行时生命周期编排。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from aetp_protocol.execution import ExecutionPlan
from aetp_protocol.ids import BusinessId, SessionId
from aetp_protocol.topics import command_topic

from agent.application.services.agent_log_facade import AgentLogFacade
from agent.application.services.artifact_upload_service import ArtifactUploadService
from agent.application.services.capability_publisher import CapabilityPublisher
from agent.application.services.execution_plan_controller import ExecutionPlanController
from agent.application.services.execution_runner import ExecutionRunner
from agent.application.services.execution_service import ExecutionService
from agent.application.services.lease_renewal_service import LeaseRenewalService
from agent.application.services.maintenance_controller import AgentMaintenanceController, restart_process
from agent.application.services.plugin_sync_controller import PluginSyncController
from agent.application.services.reconcile_service import ReconcileService
from agent.application.services.resource_provider import ResourceProviderRegistry
from agent.application.services.script_cache_service import ScriptCacheService
from agent.config import AgentSettings
from agent.domain.enums import AgentOutboxStatus
from agent.domain.ledger import Ledger
from agent.plugins.installer import PluginInstaller
from agent.plugins.registry import PluginRegistry
from common.backoff import ExponentialBackoff
from common.transport import MqttMessage, Transport

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Agent 配置、账本、Transport、注册、执行和心跳的生命周期编排。"""

    def __init__(
        self,
        settings: AgentSettings,
        transport: Transport,
        ledger: Ledger,
        *,
        execution_service: ExecutionService | None = None,
        script_cache: ScriptCacheService | None = None,
        artifact_uploader: ArtifactUploadService | None = None,
        capability_publisher: CapabilityPublisher | None = None,
        plugin_installer: PluginInstaller | None = None,
        plugin_registry: PluginRegistry | None = None,
        executor_resolver: Callable[[ExecutionPlan], object] | None = None,
        resource_providers: ResourceProviderRegistry | None = None,
        agent_log_facade: AgentLogFacade | None = None,
        maintenance_controller: AgentMaintenanceController | None = None,
        sleep: Callable[[float], asyncio.Future] | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._ledger = ledger
        self._session_id = getattr(transport, "session_id", "")
        self._publisher = capability_publisher
        self._plugin_installer = plugin_installer
        self._plugin_registry = plugin_registry
        self._executor_resolver = executor_resolver
        self._resource_providers = resource_providers
        self._agent_log_facade = agent_log_facade
        self._maintenance_controller = maintenance_controller
        self._sleep = sleep or asyncio.sleep
        self._execution_service = execution_service
        self._script_cache = script_cache
        self._artifact_uploader = artifact_uploader
        self._plugin_sync: PluginSyncController | None = None
        self._lease_renewal: LeaseRenewalService | None = None
        self._reconcile: ReconcileService | None = None
        self._execution_runner: ExecutionRunner | None = None
        self._plan_controller: ExecutionPlanController | None = None
        self._outbox_task: asyncio.Task[None] | None = None
        self._registration_task: asyncio.Task[None] | None = None
        self._lease_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._agent_log_task: asyncio.Task[None] | None = None
        self._agent_log_handler_installed = False
        self._stop_event = asyncio.Event()
        self._outbox_backoff = ExponentialBackoff()

    def _ensure_components(self) -> None:
        if self._execution_service is None:
            self._execution_service = ExecutionService(settings=self._settings, ledger=self._ledger)
        if (
            self._publisher is None
            or self._plugin_installer is None
            or self._plugin_registry is None
            or self._executor_resolver is None
        ):
            raise RuntimeError("Agent 缺少当前协议执行组件")
        publisher = self._publisher
        node_id = BusinessId(self._settings.node_id)
        if self._plugin_sync is None:
            self._plugin_sync = PluginSyncController(
                node_id,
                self._ledger,
                self._plugin_installer,
                self._plugin_registry,
                publisher,
                master_id=self._settings.master_id,
                restart=restart_process,
            )
        if self._lease_renewal is None:
            self._lease_renewal = LeaseRenewalService(
                node_id,
                self._ledger,
                publisher,
                master_id=self._settings.master_id,
            )
        if self._reconcile is None:
            self._reconcile = ReconcileService(
                node_id,
                self._ledger,
                publisher,
                master_id=self._settings.master_id,
            )
        if self._execution_runner is None:
            self._execution_runner = ExecutionRunner(
                self._settings,
                self._ledger,
                self._execution_service,
                publisher,
                self._executor_resolver,
                script_cache=self._script_cache,
                resource_providers=self._resource_providers,
                artifact_uploader=self._artifact_uploader,
            )
        if self._plan_controller is None:
            self._plan_controller = ExecutionPlanController(
                node_id,
                self._ledger,
                publisher,
                self._plugin_registry,
                script_cache=self._script_cache,
                is_registered=lambda: publisher.registered,
                master_id=self._settings.master_id,
                lease_renewal=self._lease_renewal,
                execution_runner=self._execution_runner,
                execution_service=self._execution_service,
            )
        if self._maintenance_controller is None and self._agent_log_facade is not None:
            self._maintenance_controller = AgentMaintenanceController(
                node_id,
                self._ledger,
                publisher,
                self._agent_log_facade,
                is_registered=lambda: publisher.registered,
                master_id=self._settings.master_id,
            )

    async def start(self) -> None:
        """启动 Agent 主生命周期（幂等）。"""
        if self._outbox_task is not None:
            return
        self._ensure_components()
        assert self._publisher is not None
        assert self._plugin_sync is not None
        assert self._plan_controller is not None
        assert self._lease_renewal is not None
        assert self._reconcile is not None
        if self._agent_log_facade is not None:
            root_logger = logging.getLogger()
            if self._agent_log_facade not in root_logger.handlers:
                root_logger.addHandler(self._agent_log_facade)
            self._agent_log_handler_installed = True
        self._stop_event.clear()
        self._transport.on_message(self._handle_message)
        self._transport.on_connection_change(self._handle_connection_change)
        subscriptions = [
            self._publisher.register_ack_topic(),
            self._publisher.diagnostics_command_topic(),
            self._plugin_sync.command_topic(),
            self._plan_controller.command_topic(),
            self._plan_controller.cancel_command_topic(),
            command_topic(self._settings.node_id, "lease.renewed"),
            command_topic(self._settings.node_id, "execution.reconcile_result"),
        ]
        if self._maintenance_controller is not None:
            subscriptions.extend(
                [
                    self._maintenance_controller.log_level_topic(),
                    self._maintenance_controller.drain_topic(),
                    self._maintenance_controller.restart_topic(),
                ]
            )
        await self._transport.subscribe(subscriptions)
        self._outbox_task = asyncio.create_task(self._outbox_loop())
        if self._agent_log_facade is not None:
            self._agent_log_task = asyncio.create_task(self._agent_log_loop())
        self._lease_task = asyncio.create_task(self._lease_renewal_loop())
        await self._transport.connect()
        logger.info("Agent runtime 已启动: node=%s", self._settings.node_id)

    async def stop(self) -> None:
        """停止 Agent 主生命周期（幂等）。"""
        self._stop_event.set()
        self._cancel_registration_waiter()
        for task_name in ("_lease_task", "_heartbeat_task", "_agent_log_task"):
            task = getattr(self, task_name)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                setattr(self, task_name, None)
        if self._execution_runner is not None:
            await self._execution_runner.stop()
        if self._publisher is not None and self._publisher.registered:
            try:
                await self._publisher.publish_presence(self._session(), "shutdown")
            except Exception:
                logger.debug("发送 graceful Presence 失败（已忽略）", exc_info=True)
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._outbox_task
            self._outbox_task = None
        try:
            await self._transport.disconnect()
        except Exception:
            logger.debug("断开 MQTT 异常（已忽略）", exc_info=True)
        if self._agent_log_handler_installed and self._agent_log_facade is not None:
            logging.getLogger().removeHandler(self._agent_log_facade)
            self._agent_log_handler_installed = False
        logger.info("Agent runtime 已停止: node=%s", self._settings.node_id)

    async def _handle_connection_change(self, connected: bool, session_id: str | None = None) -> None:
        if session_id:
            self._session_id = session_id
        if connected:
            assert self._publisher is not None
            self._publisher.reset_session()
            if self._plugin_sync is not None:
                self._plugin_sync.reset_session()
            if self._reconcile is not None:
                self._reconcile.reset_session()
            if self._lease_renewal is not None:
                self._lease_renewal.reset_session()
            self._publisher.enqueue_register(self._ledger, self._session())
            self._cancel_registration_waiter()
            self._registration_task = asyncio.create_task(self._wait_for_registration())
            return
        self._cancel_registration_waiter()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        if self._publisher is not None:
            self._publisher.reset_session()
        if self._plugin_sync is not None:
            self._plugin_sync.reset_session()
        if self._reconcile is not None:
            self._reconcile.reset_session()
        if self._lease_renewal is not None:
            self._lease_renewal.reset_session()

    async def _wait_for_registration(self) -> None:
        assert self._publisher is not None
        assert self._reconcile is not None
        backoff = ExponentialBackoff(
            base_delay_s=max(1.0, float(self._settings.registration_timeout_s)),
            max_delay_s=60.0,
        )
        while not self._stop_event.is_set():
            try:
                accepted = await self._publisher.wait_for_register_ack(
                    float(self._settings.registration_timeout_s)
                )
                if not accepted:
                    logger.warning("Agent 注册被 Master 拒绝")
                    return
                await self._publisher.publish_snapshot(self._session())
                self._reconcile.enqueue(self._session())
                if self._heartbeat_task is None or self._heartbeat_task.done():
                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                return
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                delay = backoff.next()
                logger.warning("Agent 注册超时，%.1fs 后重发", delay)
                await self._sleep(delay)
                if not self._stop_event.is_set():
                    self._publisher.enqueue_register(self._ledger, self._session())
            except Exception:
                logger.exception("Agent 注册等待异常")
                return

    def _cancel_registration_waiter(self) -> None:
        if self._registration_task is not None:
            self._registration_task.cancel()
            self._registration_task = None

    async def _handle_message(self, message: MqttMessage) -> None:
        assert self._publisher is not None
        if self._publisher.handle_register_ack(message, self._session()):
            return
        if self._plugin_sync is not None and message.topic == self._plugin_sync.command_topic():
            await self._plugin_sync.handle(message, self._session())
            return
        if self._plan_controller is not None:
            if message.topic == self._plan_controller.command_topic():
                await self._plan_controller.handle(message, self._session())
                return
            if message.topic == self._plan_controller.cancel_command_topic():
                await self._plan_controller.handle_cancel(message, self._session())
                return
        if self._lease_renewal is not None and message.topic == command_topic(
            self._settings.node_id,
            "lease.renewed",
        ):
            self._lease_renewal.handle_renewed(message, self._session())
            return
        if self._reconcile is not None and message.topic == command_topic(
            self._settings.node_id,
            "execution.reconcile_result",
        ):
            self._reconcile.handle_result(message, self._session())
            return
        if self._maintenance_controller is not None and message.topic in {
            self._maintenance_controller.log_level_topic(),
            self._maintenance_controller.drain_topic(),
            self._maintenance_controller.restart_topic(),
        }:
            await self._maintenance_controller.handle(message, self._session())
            return
        if self._agent_log_facade is not None and message.topic == command_topic(
            self._settings.node_id,
            "agent.log.received",
        ):
            self._publisher.handle_agent_log_received(message, self._session(), self._agent_log_facade)
            return
        if message.topic == self._publisher.diagnostics_command_topic():
            await self._publisher.handle_diagnostics_request(message, self._session())

    def _session(self) -> SessionId:
        return SessionId(self._session_id)

    async def _heartbeat_loop(self) -> None:
        assert self._publisher is not None
        while not self._stop_event.is_set():
            if self._publisher.registered:
                try:
                    await self._publisher.publish_heartbeat(self._session())
                except Exception:
                    logger.exception("Agent 心跳发布失败")
            await self._sleep(max(1.0, float(self._settings.heartbeat_interval_s)))

    async def _lease_renewal_loop(self) -> None:
        assert self._lease_renewal is not None
        while not self._stop_event.is_set():
            if self._publisher is not None and self._publisher.registered:
                try:
                    await self._lease_renewal.run_once(self._session())
                except Exception:
                    logger.exception("Lease 续租轮询失败")
            await self._sleep(max(1.0, min(float(self._settings.heartbeat_interval_s), 5.0)))

    async def _agent_log_loop(self) -> None:
        assert self._publisher is not None
        assert self._agent_log_facade is not None
        while not self._stop_event.is_set():
            if self._publisher.registered:
                try:
                    batch = self._agent_log_facade.build_batch(
                        self._session(),
                        limit=min(100, self._settings.task_log_batch_size),
                    )
                    if batch is not None:
                        self._publisher.enqueue_agent_log_batch(self._ledger, batch, self._session())
                except Exception:
                    logger.exception("Agent 结构化日志批次生成失败")
            await self._sleep(max(0.5, float(self._settings.task_log_flush_s)))

    async def _outbox_loop(self) -> None:
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
                except Exception as exc:
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
