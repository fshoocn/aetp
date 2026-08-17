"""P5.4：Agent 命令分发器（严格 Envelope 校验、Inbox 去重、先 claim 后 ACK）。

``CommandDispatcher`` 是 Agent 收到 Master 命令后的唯一处理入口：

1. **Envelope 校验**：解析 JSON → Envelope（extra=forbid 拒绝非法字段）；
2. **Topic/Sender 校验**：sender.kind=master、topic 方向=commands、message_type 与 topic 段匹配；
3. **Inbox 去重**：(origin_id=sender.id, message_id) 唯一，重复消息直接丢弃；
4. **命令路由**：run.assign → claim + ACK；run.cancel → 设置取消标志；
5. **先 claim 后 ACK**：run.assign 必须先原子 claim 成功才写 ACK outbox。

重复 assign 幂等：同一 (run_id, attempt_no) 重复派发时 claim 返回 False，
仍以当前 Run 状态回 ACK（ACK 通过 outbox 可靠发送，断网后重连补发）。

本模块只依赖 Ledger/RegistrationService 端口与协议 DTO，不接触 MQTT。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.errors import (
    InvalidSenderError,
    ProtocolError,
    TopicMismatchError,
)
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import RunAckPayload, RunAssignPayload, RunCancelPayload
from aetp_protocol.topics import (
    event_topic,
    parse_topic,
    validate_message_type_for_topic,
    validate_sender_for_topic,
)

from agent.config import AgentSettings
from agent.domain.enums import AgentOutboxStatus, AgentRunStatus
from agent.domain.ledger import AgentRun, Ledger

logger = logging.getLogger(__name__)


class CommandDispatcher:
    """Agent 命令分发器：校验 → 去重 → claim → ACK。"""

    def __init__(
        self,
        settings: AgentSettings,
        ledger: Ledger,
        *,
        is_registered: Callable[[], bool],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._is_registered = is_registered
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- 公共接口 -----------------------------------------------------------

    def handle_command(self, message) -> bool:
        """处理一条入站命令；成功返回 True，校验/去重失败返回 False。

        调用方（Runtime）传入 MqttMessage（topic + payload bytes）。
        不抛异常：所有校验失败静默返回 False，由调用方决定是否日志。
        """
        try:
            envelope = self._parse_and_validate(message.topic, message.payload)
            if envelope is None:
                return False
        except Exception:  # noqa: BLE001 - 协议错误静默忽略
            logger.warning(
                "命令校验失败: topic=%s", message.topic, exc_info=True
            )
            return False

        msg_type = envelope.message_type
        if msg_type == MessageType.RUN_ASSIGN.value:
            return self._handle_run_assign(message.topic, envelope)
        if msg_type == MessageType.RUN_CANCEL.value:
            return self._handle_run_cancel(message.topic, envelope)

        logger.debug("未处理的命令类型: %s", msg_type)
        return False

    # -- 校验 ---------------------------------------------------------------

    def _parse_and_validate(
        self, topic: str, payload: bytes
    ) -> Envelope | None:
        """严格 Envelope 校验：解析 + topic/sender/message_type 匹配。"""
        envelope = Envelope.model_validate(json.loads(payload.decode("utf-8")))
        validate_sender_for_topic(topic, envelope.sender)
        validate_message_type_for_topic(topic, MessageType(envelope.message_type))
        # commands 主题额外校验 sender.id 为已知 Master
        if envelope.sender.id != self._settings.master_id:
            raise InvalidSenderError(
                f"sender.id 不是已知 Master: {envelope.sender.id}"
                f"（期望 {self._settings.master_id}）"
            )
        return envelope

    # -- run.assign 处理 ----------------------------------------------------

    def _handle_run_assign(self, topic: str, envelope: Envelope) -> bool:
        """run.assign 完整流程：注册检查 → 校验 → 去重 → claim → ACK。"""
        # 未注册不接受命令（§9.7 规则 2）
        if not self._is_registered():
            logger.warning("run.assign 被拒绝：Agent 未注册")
            return False

        # Inbox 去重
        if not self._ledger.record_inbox(
            origin_id=envelope.sender.id,
            message_id=envelope.message_id,
            message_type=envelope.message_type,
        ):
            logger.debug(
                "run.assign 幂等忽略（inbox 去重）: message_id=%s",
                envelope.message_id,
            )
            # 重复消息仍回 ACK（幂等 ACK，断网补发）
            self._ack_run_assign(envelope, accepted=True, reason="ok (duplicate)")
            return True

        # 校验 payload
        try:
            payload = RunAssignPayload.model_validate(envelope.payload)
        except Exception:  # noqa: BLE001 - payload 格式错误
            logger.warning(
                "run.assign payload 校验失败: message_id=%s",
                envelope.message_id,
            )
            return False

        # 校验 run.assign 目标节点
        topic_info = parse_topic(topic)
        if topic_info.node_id != self._settings.node_id:
            logger.warning(
                "run.assign 目标节点不匹配: expected=%s got=%s",
                self._settings.node_id,
                topic_info.node_id,
            )
            return False

        # 原子 claim（先 claim 后 ACK）
        claimed = self._ledger.claim_run(payload.run_id, payload.attempt_no)
        if not claimed:
            # 重复派发或 attempt 冲突：仍以当前状态回 ACK
            logger.debug(
                "run.assign 幂等 ACK（claim 失败）: run_id=%s attempt=%s",
                payload.run_id,
                payload.attempt_no,
            )
            self._ack_run_assign(envelope, accepted=True, reason="ok (already claimed)")
            return True

        # claim 成功，更新为 CLAIMED 状态
        run = self._ledger.get_run(payload.run_id)
        if run is not None:
            run.status = AgentRunStatus.CLAIMED
            self._ledger.update_run(run)

        # ACK outbox（可靠发送，断网重连补发）
        self._ack_run_assign(envelope, accepted=True, reason="ok")
        logger.info(
            "run.assign 已接受: run_id=%s attempt=%s task_type=%s",
            payload.run_id,
            payload.attempt_no,
            payload.task_type,
        )
        return True

    # -- run.cancel 处理 ----------------------------------------------------

    def _handle_run_cancel(self, topic: str, envelope: Envelope) -> bool:
        """run.cancel 设置取消标志；最终以 result 为准。"""
        if not self._is_registered():
            logger.warning("run.cancel 被拒绝：Agent 未注册")
            return False

        # Inbox 去重
        if not self._ledger.record_inbox(
            origin_id=envelope.sender.id,
            message_id=envelope.message_id,
            message_type=envelope.message_type,
        ):
            logger.debug(
                "run.cancel 幂等忽略（inbox 去重）: message_id=%s",
                envelope.message_id,
            )
            return True

        try:
            payload = RunCancelPayload.model_validate(envelope.payload)
        except Exception:  # noqa: BLE001
            logger.warning(
                "run.cancel payload 校验失败: message_id=%s",
                envelope.message_id,
            )
            return False

        run = self._ledger.get_run(payload.run_id)
        if run is None:
            logger.debug(
                "run.cancel 目标 run 不存在: run_id=%s", payload.run_id
            )
            return True  # 不报错，静默忽略

        if run.status in {
            AgentRunStatus.SUCCEEDED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.FAILED,
            AgentRunStatus.TIMED_OUT,
        }:
            logger.debug(
                "run.cancel 目标 run 已终结: run_id=%s status=%s",
                payload.run_id,
                run.status,
            )
            return True  # 已终结，静默忽略

        run.cancelled = True
        self._ledger.update_run(run)
        logger.info(
            "run.cancel 已标记: run_id=%s reason=%s",
            payload.run_id,
            payload.reason,
        )
        return True

    # -- ACK 构造与入队 ------------------------------------------------------

    def _ack_run_assign(
        self,
        envelope: Envelope,
        *,
        accepted: bool,
        reason: str,
    ) -> None:
        """构造 run.ack Envelope 并写入 outbox（QoS 1 可靠发送）。"""
        payload_dict = envelope.payload
        run_id = payload_dict.get("run_id", "")
        attempt_no = payload_dict.get("attempt_no", 0)
        dispatch_id = payload_dict.get("dispatch_id", "")

        ack = RunAckPayload(
            run_id=run_id,
            attempt_no=attempt_no,
            dispatch_id=dispatch_id,
            accepted=accepted,
            reason=reason,
        )
        ack_envelope = Envelope(
            message_id=uuid.uuid4().hex,
            message_type=MessageType.RUN_ACK.value,
            sent_at=self._now(),
            sender=Sender(
                kind=SenderKind.AGENT,
                id=self._settings.node_id,
                session_id="",  # session 由 outbox publisher 发送时确定
            ),
            correlation_id=envelope.message_id,
            trace_id=self._settings.node_id,
            payload=ack.model_dump(mode="json"),
        )
        topic = event_topic(self._settings.node_id, "ack")
        outbox_id = f"run-ack:{run_id}:{attempt_no}"
        self._ledger.enqueue_outbox(
            outbox_id, topic, ack_envelope.model_dump(mode="json")
        )
