"""Shard 调度应用服务（P4.6，§18.5/§18.6，D-14/D-20/D-23）。

应用服务负责事务、项目节点绑定和持久化；候选排序与并发规则全部委托给
``master.domain.scheduler.ShardScheduler``。每次成功调度同时写入：

1. Shard 状态 ``pending/waiting_recovery -> dispatching``；
2. 新的 ``ShardAttempt``（历史 Attempt 不覆盖）；
3. 一个 QoS 1 的 ``run.assign`` Outbox 消息。

槽位不足时不伪造失败，Shard 保持待调度状态；所有在线绑定节点都不满足
能力时才抛出 ``NODE_CAPABILITY_MISMATCH``。未绑定目标节点始终在事务内硬拒绝。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from aetp_protocol.capabilities import DeviceAllocation, SwitchRouteAllocation
from aetp_protocol.envelope import PROTOCOL_VERSION, Envelope, Sender, SenderKind
from aetp_protocol.ids import new_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import PluginPackageRef, RunAssignPayload
from aetp_protocol.topics import command_topic

from master.application.errors import (
    NodeCapabilityMismatchError,
    ProjectAccessDeniedError,
    RunNotFoundError,
    ScriptNotFoundError,
    TaskNotFoundError,
)
from master.application.services.capability_service import CapabilityService
from master.domain.capability import list_capability_paths
from master.domain.enums import (
    DeviceStatus,
    RunStatus,
    ShardAttemptStatus,
    ShardStatus,
)
from master.domain.models import (
    Node,
    OutboxMessage,
    RunShard,
    ShardAttempt,
    TaskRun,
    TestScript,
    TestTask,
)
from master.domain.repositories import UnitOfWork
from master.domain.resources import (
    NodeSchedulingState,
    ResourceAssignment,
    SwitchRoute,
)
from master.domain.scheduler import ShardScheduler
from master.domain.state_machine import assert_transition, next_attempt_no
from master.domain.time import utcnow


@dataclass(frozen=True)
class ScheduledShard:
    """一次成功写入数据库和 Outbox 的派发记录。"""

    shard_id: str
    attempt_id: str
    attempt_no: int
    node_id: str
    device_ids: tuple[str, ...]
    outbox_id: str


@dataclass(frozen=True)
class ScheduleResult:
    """一次调度轮询的结果。"""

    run_id: str
    scheduled: tuple[ScheduledShard, ...] = ()
    pending_shard_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SchedulerConfig:
    """Shard 调度器外部回调配置，聚合可选参数。"""

    download_url_builder: Callable[[str], str] | None = None
    artifact_upload_url_builder: Callable[[str, str, str, str], str] | None = None
    plugin_ref_builder: Callable[[str, str], PluginPackageRef | None] | None = None


class ShardSchedulerService:
    """将一个 Run 的可调度 Shard 物化为 Attempt + run.assign Outbox。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        scheduler: ShardScheduler | None = None,
        capability_service: CapabilityService | None = None,
        master_id: str = "aetp-master",
        config: SchedulerConfig | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._scheduler = scheduler or ShardScheduler()
        self._capability = capability_service or CapabilityService()
        self._master_id = master_id
        cfg = config or SchedulerConfig()
        self._download_url_builder = cfg.download_url_builder
        self._artifact_upload_url_builder = cfg.artifact_upload_url_builder
        self._plugin_ref_builder = cfg.plugin_ref_builder

    def schedule_run(self, run_id: str) -> ScheduleResult:
        """为 Run 尽可能派发 pending Shard。

        一个事务内按 Shard 顺序检查节点设备状态。设备忙或离线时 Shard
        保持 pending，不创建 Attempt 或 Outbox；重复调用对已有活动 Attempt
        幂等，不会重复创建 Attempt 或 Outbox。
        """

        with self._uow_factory() as uow:
            run, task, script = self._load_context(uow, run_id)
            target_nodes = self._target_nodes(uow, task)
            states = self._states(target_nodes)
            self._raise_if_capabilities_unavailable(target_nodes, script, task)

            scheduled: list[ScheduledShard] = []
            pending: list[str] = []

            # 批量预加载所有 attempts，按 shard_id 分组，避免 N+1 查询
            all_attempts = uow.shard_attempts.list_by_run(run.run_id)
            attempts_by_shard: dict[str, list[ShardAttempt]] = {}
            for a in all_attempts:
                attempts_by_shard.setdefault(a.shard_id, []).append(a)

            for shard in uow.run_shards.list_by_run(run.run_id):
                attempts = attempts_by_shard.get(shard.shard_id, [])
                if not self._is_dispatchable(shard, attempts):
                    continue

                # 派发耗尽收敛：最新 attempt 已失败且 failover 不允许时，
                # Shard 进入 FAILED 终态，不再无限停留在 dispatching
                # （否则 Agent 拒绝后 Run 会永久卡在"已派发"，§8.4）。
                if self._is_dispatch_exhausted(shard, attempts, task):
                    assert_transition(shard.status, ShardStatus.FAILED)
                    shard.status = ShardStatus.FAILED
                    shard.final_node = (
                        max(attempts, key=lambda a: a.attempt_no).node_id
                    )
                    uow.run_shards.update(shard)
                    continue

                assignment = self._select_assignment(
                    script=script,
                    task=task,
                    states=states,
                    attempts=attempts,
                )
                if assignment is None:
                    pending.append(shard.shard_id)
                    continue
                node = assignment.node
                states[node.node_id] = self._scheduler.reserve_devices(states[node.node_id], assignment.device_ids)

                dispatch = self._persist_dispatch(
                    uow=uow,
                    run=run,
                    task=task,
                    script=script,
                    shard=shard,
                    assignment=assignment,
                    attempt_no=next_attempt_no(a.attempt_no for a in attempts),
                )
                scheduled.append(dispatch)

            if scheduled and run.status is RunStatus.CREATED:
                assert_transition(run.status, RunStatus.DISPATCHED)
                run.status = RunStatus.DISPATCHED
                uow.task_runs.update(run)

            # Run 失败投影：所有 Shard 均终态失败（派发耗尽）时，Run 收敛到
            # FAILED 终态，避免 Agent 拒绝后永久卡在"已派发"。
            self._project_run_failure_if_all_shards_failed(uow, run)

            return ScheduleResult(
                run_id=run.run_id,
                scheduled=tuple(scheduled),
                pending_shard_ids=tuple(pending),
            )

    def _load_context(self, uow: UnitOfWork, run_id: str) -> tuple[TaskRun, TestTask, TestScript]:
        run = uow.task_runs.get_by_run_id(run_id)
        if run is None:
            raise RunNotFoundError(f"Run 不存在: {run_id}")
        task = uow.test_tasks.get_by_task_id(run.task_id, run.project_id)
        if task is None:
            raise TaskNotFoundError(f"任务定义不存在: {run.task_id}")
        script = uow.test_scripts.get_by_script_id(task.script_id)
        if script is None or script.project_id != run.project_id or script.version != task.script_version:
            raise ScriptNotFoundError(f"脚本版本不存在或不属于当前项目: {task.script_id} v{task.script_version}")
        return run, task, script

    @staticmethod
    def _target_nodes(uow: UnitOfWork, task: TestTask) -> list[Node]:
        bindings = uow.bindings.list_with_nodes(task.project_id)
        bound_ids = {binding.node_id for binding in bindings}
        invalid = sorted(set(task.node_ids) - bound_ids)
        if invalid:
            raise ProjectAccessDeniedError("任务目标节点不在项目绑定范围（D-23）: " + ", ".join(invalid))

        enabled_binding_ids = {binding.node_id for binding in bindings if binding.enabled and binding.node_enabled}
        selected_ids = set(task.node_ids) if task.node_ids else enabled_binding_ids
        return [
            node
            for node_id in sorted(selected_ids & enabled_binding_ids)
            if (node := uow.nodes.get_by_id(node_id)) is not None
        ]

    def _states(
        self,
        nodes: Iterable[Node],
    ) -> dict[str, NodeSchedulingState]:
        return {node.node_id: NodeSchedulingState(node=node) for node in nodes}

    def _raise_if_capabilities_unavailable(self, nodes: list[Node], script: TestScript, task: TestTask) -> None:
        online_nodes = [node for node in nodes if node.online and node.enabled]
        capable = [
            node for node in online_nodes if self._capability.evaluate(node, script.hardware_requirements).matched
        ]
        if online_nodes and not capable:
            first = online_nodes[0]
            match = self._capability.evaluate(first, script.hardware_requirements)
            raise NodeCapabilityMismatchError(
                first.node_id,
                match.failures or (f"任务 {task.task_id} 无满足硬件要求的节点",),
                available=list_capability_paths(first.capabilities),
            )
        if (
            capable
            and script.hardware_requirements.devices
            and not any(
                self._scheduler.supports_resources(node, script.hardware_requirements.devices) for node in capable
            )
        ):
            first = capable[0]
            raise NodeCapabilityMismatchError(
                first.node_id,
                (
                    "没有节点具备脚本要求的完整物理设备集合: "
                    + ", ".join(requirement.resource_type for requirement in script.hardware_requirements.devices),
                ),
                available=list_capability_paths(first.capabilities),
            )

    def _select_assignment(
        self,
        *,
        script: TestScript,
        task: TestTask,
        states: dict[str, NodeSchedulingState],
        attempts: list[ShardAttempt],
    ) -> ResourceAssignment | None:
        latest = max(attempts, key=lambda attempt: attempt.attempt_no, default=None)
        if latest is not None and latest.status in {
            ShardAttemptStatus.FAILED,
            ShardAttemptStatus.TIMED_OUT,
        }:
            if not self._failover_allowed(task, latest.attempt_no):
                return None
            return self._scheduler.select_failover_assignment(
                requirements=script.hardware_requirements,
                candidates=states.values(),
                attempts=attempts,
            )
        return self._scheduler.select_assignment(
            requirements=script.hardware_requirements,
            candidates=states.values(),
        )

    @staticmethod
    def _is_dispatchable(shard: RunShard, attempts: list[ShardAttempt]) -> bool:
        if shard.status in {ShardStatus.PENDING, ShardStatus.WAITING_RECOVERY}:
            return not any(
                attempt.status
                in {
                    ShardAttemptStatus.CREATED,
                    ShardAttemptStatus.DISPATCHED,
                    ShardAttemptStatus.ACKED,
                    ShardAttemptStatus.RUNNING,
                }
                for attempt in attempts
            )
        if shard.status is ShardStatus.DISPATCHING:
            return bool(attempts) and max(attempts, key=lambda attempt: attempt.attempt_no).status in {
                ShardAttemptStatus.FAILED,
                ShardAttemptStatus.TIMED_OUT,
            }
        return False

    @staticmethod
    def _failover_allowed(task: TestTask, latest_attempt_no: int) -> bool:
        policy = task.retry_policy
        return bool(policy.get("failover_nodes", False)) and latest_attempt_no < _positive_int(
            policy.get("max_attempts"), 1
        )

    @staticmethod
    def _is_dispatch_exhausted(
        shard: RunShard,
        attempts: list[ShardAttempt],
        task: TestTask,
    ) -> bool:
        """判断 Shard 是否已派发耗尽（应进入 FAILED 终态而非继续等待）。

        仅当 Shard 处于 DISPATCHING 且最新 attempt 已终态失败/超时、且
        failover 不允许时视为耗尽。其余情况（首次派发、无 attempt、设备
        暂时忙/离线、failover 仍可继续）返回 False，保持 pending/重派。
        """
        if shard.status is not ShardStatus.DISPATCHING:
            return False
        if not attempts:
            return False
        latest = max(attempts, key=lambda attempt: attempt.attempt_no)
        if latest.status not in {
            ShardAttemptStatus.FAILED,
            ShardAttemptStatus.TIMED_OUT,
        }:
            return False
        return not ShardSchedulerService._failover_allowed(task, latest.attempt_no)

    @staticmethod
    def _project_run_failure_if_all_shards_failed(
        uow: UnitOfWork, run: TaskRun
    ) -> None:
        """所有 Shard 均终态失败（派发耗尽）时，把 Run 收敛到 FAILED 终态。

        Agent 在下载/准备阶段 reject 后不会上报 run.result，因此 Run 无法
        通过结果投影进入失败终态；此处兜底：所有 Shard 均为 FAILED/TIMED_OUT/
        CANCELLED 等终态且至少一个失败时，Run 投影为 FAILED（§5.4 状态机）。
        """
        if run.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
            RunStatus.LOST,
        }:
            return
        shards = uow.run_shards.list_by_run(run.run_id)
        if not shards:
            return
        terminal = {
            ShardStatus.SUCCEEDED,
            ShardStatus.FAILED,
            ShardStatus.CANCELLED,
            ShardStatus.TIMED_OUT,
        }
        if not all(shard.status in terminal for shard in shards):
            return
        # 全部终态且无任一成功 → Run 失败（避免全 cancelled 误判为 failed）
        if any(shard.status is ShardStatus.SUCCEEDED for shard in shards):
            return
        assert_transition(run.status, RunStatus.FAILED)
        run.status = RunStatus.FAILED
        run.finished_at = utcnow()
        uow.task_runs.update(run)

    def _persist_dispatch(
        self,
        *,
        uow: UnitOfWork,
        run: TaskRun,
        task: TestTask,
        script: TestScript,
        shard: RunShard,
        assignment: ResourceAssignment,
        attempt_no: int,
    ) -> ScheduledShard:
        if shard.status is not ShardStatus.DISPATCHING:
            assert_transition(shard.status, ShardStatus.DISPATCHING)
            shard.status = ShardStatus.DISPATCHING
            uow.run_shards.update(shard)

        attempt = ShardAttempt(
            attempt_id=new_id(),
            shard_id=shard.shard_id,
            attempt_no=attempt_no,
            node_id=assignment.node.node_id,
            device_ids=list(assignment.device_ids),
            status=ShardAttemptStatus.DISPATCHED,
        )
        uow.shard_attempts.add(attempt)
        for device in assignment.devices:
            device.status = DeviceStatus.BUSY
            uow.devices.update(device)
        outbox_id = new_id()
        script_ref = dict(run.script_ref)
        if self._download_url_builder is not None:
            script_ref["download_url"] = self._download_url_builder(script.script_id)
        artifact_upload_url = None
        if self._artifact_upload_url_builder is not None:
            artifact_upload_url = (
                self._artifact_upload_url_builder(
                    run.run_id,
                    run.project_id,
                    assignment.node.node_id,
                    shard.shard_id,
                )
                or None
            )
        plugin_ref = (
            self._plugin_ref_builder(task.task_type, script.plugin_version)
            if self._plugin_ref_builder is not None
            else None
        )
        payload = RunAssignPayload(
            project_id=run.project_id,
            task_id=run.task_id,
            shard_id=shard.shard_id,
            shard_index=shard.shard_index,
            run_id=run.run_id,
            attempt_no=attempt_no,
            device_allocations=[
                DeviceAllocation(
                    device_id=device.device_id,
                    resource_type=device.capability.resource_type,
                    labels=dict(device.capability.labels),
                    switch_route=_switch_route_allocation(assignment.routes_by_device.get(device.device_id)),
                )
                for device in assignment.devices
            ],
            dispatch_id=attempt.attempt_id,
            task_type=task.task_type,
            plugin_version=script.plugin_version,
            plugin_ref=plugin_ref,
            script_ref=script_ref,
            artifact_upload_url=artifact_upload_url,
            case_keys=list(shard.case_keys),
            execution_params=dict(shard.execution_params),
            timeout_s=task.timeout_s,
        )
        envelope = Envelope(
            protocol_version=PROTOCOL_VERSION,
            message_id=new_id(),
            message_type=MessageType.RUN_ASSIGN.value,
            sent_at=utcnow(),
            sender=Sender(
                kind=SenderKind.MASTER,
                id=self._master_id,
                session_id=self._master_id,
            ),
            trace_id=run.run_id,
            payload=payload.model_dump(mode="json"),
        )
        uow.outbox_messages.enqueue(
            OutboxMessage(
                outbox_id=outbox_id,
                aggregate_type="shard_attempt",
                aggregate_id=attempt.attempt_id,
                topic=command_topic(assignment.node.node_id, "assign"),
                payload=envelope.model_dump(mode="json"),
            )
        )
        return ScheduledShard(
            shard_id=shard.shard_id,
            attempt_id=attempt.attempt_id,
            attempt_no=attempt_no,
            node_id=assignment.node.node_id,
            device_ids=assignment.device_ids,
            outbox_id=outbox_id,
        )

    def release_devices(self, device_ids: Iterable[str]) -> None:
        """释放已完成/失败 Attempt 占用的全部设备。

        结果、取消或派发耗尽处理器应在确认 Attempt 结束后调用；Node 只
        执行 Master 已下发的命令，不负责队列和设备排队。
        """

        with self._uow_factory() as uow:
            for device_id in set(device_ids):
                device = uow.devices.get_by_id(device_id)
                if device is None:
                    continue
                device.status = DeviceStatus.ONLINE if device.online else DeviceStatus.OFFLINE
                uow.devices.update(device)

    def release_device(self, device_id: str) -> None:
        """兼容单资源调用方，转发到批量释放。"""

        self.release_devices((device_id,))


def _switch_route_allocation(
    route: SwitchRoute | None,
) -> SwitchRouteAllocation | None:
    """将领域切换路径转换为协议分配对象。"""

    if route is None:
        return None
    return SwitchRouteAllocation(
        switch_device_id=route.switch_device_id,
        port=route.port,
    )


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value
