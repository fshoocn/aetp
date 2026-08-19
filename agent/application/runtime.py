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
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from aetp_protocol.envelope import Envelope
from aetp_protocol.message_types import MessageType
from aetp_protocol.topics import command_topic

from agent.application.services.command_dispatcher import CommandDispatcher
from agent.application.services.artifact_upload_service import ArtifactUploadService
from agent.application.services.execution_service import ExecutionService
from agent.application.services.registration_service import (
    RegistrationRejectedError,
    RegistrationService,
    RegistrationTimeoutError,
)
from agent.application.services.run_orchestrator import RunOrchestrator
from agent.application.services.script_cache_service import ScriptCacheService
from agent.application.services.script_preflight_service import (
    ScriptPreflightService,
)
from agent.config import AgentSettings
from agent.domain.enums import AgentOutboxStatus
from agent.domain.ledger import Ledger
from common.backoff import ExponentialBackoff
from common.transport import MqttMessage, Transport

if TYPE_CHECKING:
    from agent.plugins import AgentPluginRegistry
    from agent.plugins.installer import PluginPackageInstaller

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
        plugin_registry: "AgentPluginRegistry | None" = None,
        plugin_installer: "PluginPackageInstaller | None" = None,
        script_cache: ScriptCacheService | None = None,
        artifact_uploader: ArtifactUploadService | None = None,
        script_preflight: ScriptPreflightService | None = None,
        execution_service: ExecutionService | None = None,
        sleep: Callable[[float], asyncio.Future] | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._ledger = ledger
        self._registration = registration
        self._sleep = sleep or asyncio.sleep
        self._outbox_task: asyncio.Task[None] | None = None
        self._registration_task: asyncio.Task[None] | None = None
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
            self._execution_service = ExecutionService(
                settings=self._settings, ledger=self._ledger
            )
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
        await self._transport.subscribe(
            [
                command_topic(self._settings.node_id, "register-ack"),
                command_topic(self._settings.node_id, "assign"),
                command_topic(self._settings.node_id, "cancel"),
                command_topic(self._settings.node_id, "verify"),
                command_topic(self._settings.node_id, "parse"),
            ]
        )
        self._outbox_task = asyncio.create_task(self._outbox_loop())
        await self._transport.connect()
        logger.info("Agent runtime 已启动: node=%s", self._settings.node_id)

    async def stop(self) -> None:
        """停止 Agent 主生命周期（幂等）。"""
        self._stop_event.set()
        await self._registration.stop_heartbeat()
        self._cancel_registration_waiter()
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except asyncio.CancelledError:
                pass
            self._outbox_task = None
        await self._transport.disconnect()
        logger.info("Agent runtime 已停止: node=%s", self._settings.node_id)

    async def _handle_connection_change(
        self, connected: bool, session_id: str | None = None
    ) -> None:
        """连接成功触发重新注册，断开撤销注册状态。"""
        await self._registration.handle_connection_change(connected, session_id)
        if connected:
            self._cancel_registration_waiter()
            self._registration_task = asyncio.create_task(
                self._wait_for_registration()
            )
        else:
            self._cancel_registration_waiter()

    async def _wait_for_registration(self) -> None:
        """等待 ACK；超时/拒绝后主动断开，交给 Transport 重连。"""
        try:
            await self._registration.wait_for_register_ack()
            await self._registration.start_heartbeat()
        except asyncio.CancelledError:
            raise
        except (RegistrationTimeoutError, RegistrationRejectedError) as exc:
            logger.warning("Agent 注册失败，将触发重连: %s", exc)
            await self._transport.disconnect()
            if not self._stop_event.is_set():
                await self._transport.connect()
        except Exception:  # noqa: BLE001 - 生命周期失败统一交给重连
            logger.exception("Agent 注册等待异常")
            await self._transport.disconnect()
            if not self._stop_event.is_set():
                await self._transport.connect()

    def _cancel_registration_waiter(self) -> None:
        """取消旧 session 的 ACK 等待任务。"""
        if self._registration_task is not None:
            self._registration_task.cancel()
            self._registration_task = None

    async def _handle_message(self, message: MqttMessage) -> None:  # noqa: C901
        """路由入站消息到 register-ack / 命令分发器 / 脚本预检（P5.4/P5.7）。"""
        assert self._dispatcher_obj is not None, "组件未初始化，请先调用 start()"
        if self._registration.handle_register_ack(message):
            return
        # P5.7：script.verify / script.parse 命令路由到脚本预检服务
        if self._route_script_preflight(message):
            return
        # P5.4：run.assign / run.cancel 命令路由
        self._dispatcher_obj.handle_command(message)

    def _route_script_preflight(self, message: MqttMessage) -> bool:
        """解析入站命令；若为 script.verify/script.parse 则交给预检服务。"""
        if self._script_preflight_obj is None:
            return False
        try:
            envelope = Envelope.model_validate(
                json.loads(message.payload.decode("utf-8"))
            )
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
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            entries = self._ledger.claim_due_outbox(100, now)
            for entry in entries:
                try:
                    await self._transport.publish(
                        entry.topic,
                        json.dumps(entry.payload).encode("utf-8"),
                        qos=1,
                    )
                except Exception as exc:  # noqa: BLE001 - 单条失败不阻塞批次
                    retry_at = now + timedelta(
                        seconds=min(60, 2 ** min(entry.attempts + 1, 6))
                    )
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
