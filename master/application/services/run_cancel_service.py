"""Run 取消服务（P8.1，§5.4/§8.4）。

用户请求取消一个正在执行的 Run：
1. 校验 Run 处于可取消状态（dispatched/acked/running）；
2. 向所有活跃 Shard 的当前节点发送 `run.cancel` outbox；
3. Agent 收到后设置取消标志，插件在安全点释放硬件并报告 cancelled 结果；
4. Run 状态由 Agent 结果投影决定（§5.4：Run 无 cancelling 中间态）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Callable

from aetp_protocol.envelope import PROTOCOL_VERSION, Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import RunCancelPayload
from aetp_protocol.topics import command_topic

from master.application.errors import RunNotFoundError
from master.domain.enums import OutboxStatus, RunStatus, ShardStatus
from master.domain.models import OutboxMessage
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)

_CANCELABLE_RUN_STATUSES = frozenset(
    {RunStatus.DISPATCHED, RunStatus.ACKED, RunStatus.RUNNING}
)
_ACTIVE_SHARD_STATUSES = frozenset(
    {
        ShardStatus.PENDING,
        ShardStatus.DISPATCHING,
        ShardStatus.RUNNING,
        ShardStatus.WAITING_RECOVERY,
    }
)


@dataclass(frozen=True)
class CancelResult:
    """取消操作的产出。"""

    run_id: str
    project_id: str
    task_id: str
    cancelled_shards: int
    already_terminal: bool


class RunCancelService:
    """向活跃 Shard 节点发送 run.cancel 命令。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        master_id: str = "aetp-master",
    ) -> None:
        self._uow_factory = uow_factory
        self._master_id = master_id

    def cancel(
        self,
        run_id: str,
        *,
        project_id: str,
        reason: str = "用户请求取消",
    ) -> CancelResult:
        """取消一个 Run：向活跃 Shard 节点发 outbox。"""
        with self._uow_factory() as uow:
            run = uow.task_runs.get_by_run_id(run_id, project_id)
            if run is None:
                raise RunNotFoundError(f"Run 不存在或不属于当前项目: {run_id}")

            # 已终态：幂等返回
            if run.status in {
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.TIMED_OUT,
                RunStatus.LOST,
            }:
                logger.info(
                    "Run 已终态，取消幂等忽略: run_id=%s status=%s",
                    run_id,
                    run.status.value,
                )
                return CancelResult(
                    run_id=run_id,
                    project_id=project_id,
                    task_id=run.task_id,
                    cancelled_shards=0,
                    already_terminal=True,
                )

            # 找所有活跃 Shard
            shards = uow.run_shards.list_by_run(run_id)
            active_shards = [s for s in shards if s.status in _ACTIVE_SHARD_STATUSES]

            # 按 final_node 去重发送
            sent_nodes: set[str] = set()
            for shard in active_shards:
                node_id = shard.final_node
                if not node_id or node_id in sent_nodes:
                    continue
                sent_nodes.add(node_id)

                cancel_payload = RunCancelPayload(
                    run_id=run_id,
                    reason=reason,
                )
                envelope = Envelope(
                    message_id=uuid.uuid4().hex,
                    message_type=MessageType.RUN_CANCEL.value,
                    sent_at=utcnow(),
                    sender=Sender(
                        kind=SenderKind.MASTER,
                        id=self._master_id,
                        session_id=self._master_id,
                    ),
                    trace_id=run_id,
                    payload=cancel_payload.model_dump(mode="json"),
                )
                outbox_id = f"run-cancel:{run_id}:{node_id}"
                uow.outbox_messages.enqueue(
                    OutboxMessage(
                        outbox_id=outbox_id,
                        aggregate_type="task_run",
                        aggregate_id=run_id,
                        topic=command_topic(node_id, "cancel"),
                        payload=envelope.model_dump(mode="json"),
                        qos=1,
                        status=OutboxStatus.PENDING,
                    )
                )
                logger.info(
                    "run.cancel 已入 outbox: run_id=%s node=%s shard_count=%d",
                    run_id,
                    node_id,
                    sum(1 for s in active_shards if s.final_node == node_id),
                )

        return CancelResult(
            run_id=run_id,
            project_id=project_id,
            task_id=run.task_id,
            cancelled_shards=len(active_shards),
            already_terminal=False,
        )
