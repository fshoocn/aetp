""" 多脚本 Run 调度和 ExecutionPlan 生成。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from aetp_protocol.capabilities import ResourceCapability, SwitchRouteAllocation
from aetp_protocol.execution import ExecutionPlan, ExecutorRef, PlanResourceBinding, ResourceRequirement
from aetp_protocol.ids import BusinessId, PluginId, SemVer, SessionId, Sha256, new_id, stable_id
from aetp_protocol.plan_hash import with_plan_hash
from aetp_protocol.plugin_types import PluginDistributionRef, PluginStatus
from aetp_protocol.task import RunScriptSnapshot, RunSnapshot

from master.application.services.node_matching_service import NodeMatchingService
from master.application.services.plan_materialization_service import (
    MaterializedPlan,
    PlanMaterializationService,
)
from master.domain.enums import RunStatus, ShardAttemptStatus, ShardStatus
from master.domain.models import RunShard, TaskRun
from master.domain.repositories import UnitOfWork
from master.domain.state_machine import assert_transition
from master.domain.time import utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduledPlan:
    plan: ExecutionPlan
    materialized: MaterializedPlan


@dataclass(frozen=True)
class ScheduleResult:
    run_id: str
    scheduled: tuple[ScheduledPlan, ...] = ()
    pending_shard_ids: tuple[str, ...] = ()
    cancelled_shard_ids: tuple[str, ...] = ()


class SchedulerService:
    """以 Snapshot 为唯一输入的  脚本级调度器。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        node_matching: NodeMatchingService,
        materializer: PlanMaterializationService,
        *,
        script_url_builder: Callable[[str], str] | None = None,
        plugin_url_builder: Callable[[PluginId, SemVer], str] | None = None,
        artifact_url_builder: Callable[[str, str, str, str, str], str] | None = None,
        now: Callable[[], datetime] | None = None,
        plan_ttl_s: int = 3600,
    ) -> None:
        if plan_ttl_s <= 0:
            raise ValueError("plan_ttl_s 必须大于 0")
        self._uow_factory = uow_factory
        self._node_matching = node_matching
        self._materializer = materializer
        self._script_url_builder = script_url_builder
        self._plugin_url_builder = plugin_url_builder
        self._artifact_url_builder = artifact_url_builder
        self._now = now or utcnow
        self._plan_ttl = timedelta(seconds=plan_ttl_s)

    def schedule_run(self, run_id: BusinessId) -> ScheduleResult:
        """为一个  Run 派发当前可用脚本 Shard。"""
        with self._uow_factory() as uow:
            run = uow.task_runs.get_by_run_id(run_id.root)
            if run is None or run.snapshot is None:
                raise KeyError(f" Run 不存在或缺少 Snapshot: {run_id.root}")
            snapshot = run.snapshot
            shards = uow.run_shards.list_by_run(run_id.root)
            by_binding = self._scripts_by_binding(snapshot)
            eligible_bindings = self._eligible_bindings(snapshot, shards)
            cancelled: list[str] = []
            if snapshot.execution_mode == "sequence" and snapshot.stop_on_failure:
                cancelled = self._cancel_after_failure(uow, shards, snapshot)
                shards = uow.run_shards.list_by_run(run_id.root)
                eligible_bindings = self._eligible_bindings(snapshot, shards)

            scheduled: list[ScheduledPlan] = []
            pending: list[str] = []
            for shard in shards:
                if shard.script_binding_id not in eligible_bindings:
                    continue
                if self._mark_exhausted_if_needed(uow, shard, snapshot):
                    continue
                if not self._is_dispatchable(uow, shard, snapshot):
                    continue
                script = by_binding[shard.script_binding_id]
                plan = self._build_plan(uow, run, snapshot, script, shard)
                if plan is None:
                    pending.append(shard.shard_id)
                    continue
                try:
                    materialized = self._materializer.materialize(plan)
                except ValueError:
                    # 资源/Session 竞态由下一轮调度重新匹配，不能伪造失败。
                    pending.append(shard.shard_id)
                    continue
                scheduled.append(ScheduledPlan(plan=plan, materialized=materialized))

            self._project_cancelled_run_if_terminal(uow, run)
            return ScheduleResult(
                run_id=run_id.root,
                scheduled=tuple(scheduled),
                pending_shard_ids=tuple(pending),
                cancelled_shard_ids=tuple(cancelled),
            )

    def reschedule_pending_runs(self, *, node_id: str | None = None) -> int:
        """节点上线后重新轮询仍有  Shard 的非终态 Run。"""
        with self._uow_factory() as uow:
            run_ids = [
                run.run_id
                for run in uow.task_runs.list_non_terminal(limit=1000)
                if run.snapshot is not None
            ]
        scheduled_count = 0
        for run_id in run_ids:
            result = self.schedule_run(BusinessId(run_id))
            scheduled_count += len(result.scheduled)
        if scheduled_count:
            logger.info(
                " 节点上线触发补偿调度: node=%s scheduled=%d",
                node_id or "*",
                scheduled_count,
            )
        return scheduled_count

    def _build_plan(
        self,
        uow: UnitOfWork,
        run: TaskRun,
        snapshot: RunSnapshot,
        script: RunScriptSnapshot,
        shard: RunShard,
    ) -> ExecutionPlan | None:
        matches = self._node_matching.match(script.requirement, node_ids=snapshot.node_ids)
        attempts = uow.shard_attempts.list_by_shard(shard.shard_id)
        used_nodes = {
            attempt.node_id
            for attempt in attempts
            if attempt.status in {
                ShardAttemptStatus.DISPATCHED,
                ShardAttemptStatus.ACKED,
                ShardAttemptStatus.RUNNING,
            }
            or (snapshot.retry_policy.failover_nodes and attempt.status in {
                ShardAttemptStatus.FAILED,
                ShardAttemptStatus.TIMED_OUT,
            })
        }
        selected = next((match for match in matches if match.matched and match.node_id.root not in used_nodes), None)
        if selected is None:
            return None
        node = uow.nodes.get_by_id(selected.node_id.root)
        if node is None or node.id is None:
            return None
        maintenance_locks = getattr(uow, "maintenance_locks", None)
        if maintenance_locks is not None and maintenance_locks.is_locked(selected.node_id):
            return None
        session = uow.node_sessions.get_current(node.id)
        if session is None or not node.online or not node.enabled:
            return None
        attempt_no = max((attempt.attempt_no for attempt in attempts), default=0) + 1
        attempt_id = BusinessId(new_id())
        now = self._now()
        deadline = now + self._plan_ttl
        plan_id = BusinessId(new_id())
        resource_bindings = self._resource_bindings(
            uow,
            selected.node_id,
            script.requirement.resources,
            plan_id,
            deadline,
        )
        if resource_bindings is None:
            return None
        plugin_package = self._plugin_package(uow, script)
        source = script.source
        if self._script_url_builder is not None:
            source = source.model_copy(update={"download_url": self._script_url_builder(source.script_id.root)})
        artifact_url = None
        if self._artifact_url_builder is not None:
            artifact_url = self._artifact_url_builder(
                run.run_id,
                run.project_id,
                selected.node_id.root,
                shard.shard_id,
                attempt_id.root,
            ) or None
        plan = ExecutionPlan(
            schema_version=2,
            plan_id=plan_id,
            plan_hash=Sha256("0" * 64),
            run_id=BusinessId(run.run_id),
            task_id=snapshot.task_id,
            script_binding_id=script.binding_id,
            script_definition_id=script.script_definition_id,
            shard_id=BusinessId(shard.shard_id),
            attempt_id=attempt_id,
            attempt_no=attempt_no,
            project_id=BusinessId(run.project_id),
            node_id=selected.node_id,
            target_session_id=SessionId(session.session_id),
            executor=ExecutorRef(
                plugin_id=script.executor.plugin_id,
                version=script.executor.version,
            ),
            plugin_package=plugin_package,
            resource_bindings=resource_bindings,
            script=source,
            configuration=script.configuration,
            execution_parameters=shard.execution_params,
            case_keys=tuple(shard.case_keys),
            artifact_upload_url=artifact_url,
            created_at=now,
            deadline_at=deadline,
        )
        return with_plan_hash(plan)

    def _resource_bindings(
        self,
        uow: UnitOfWork,
        node_id: BusinessId,
        requirements: tuple[ResourceRequirement, ...],
        plan_id: BusinessId,
        deadline: datetime,
    ) -> tuple[PlanResourceBinding, ...] | None:
        """从最新能力快照选择具体 ready 资源，失败时整组返回 pending。"""
        if not requirements:
            return ()
        snapshot_record = uow.node_capability_snapshots.get_latest(node_id)
        if snapshot_record is None:
            return None
        used: set[str] = set()
        bindings: list[PlanResourceBinding] = []
        expires_at = min(deadline, self._now() + timedelta(minutes=5))
        for requirement in requirements:
            candidates = [
                resource
                for resource in snapshot_record.snapshot.resources
                if resource.resource_id.root not in used
                and self._resource_matches(resource, requirement)
                and uow.resource_leases.get_active_by_resource(resource.resource_id) is None
            ]
            candidates.sort(
                key=lambda resource: self._preferred_label_score(resource.labels, requirement.preferred_labels),
                reverse=True,
            )
            selected: list[tuple[ResourceCapability, SwitchRouteAllocation | None]] = []
            for resource in candidates:
                route = self._switch_route(resource, requirement)
                if not self._labels_match(resource.labels, requirement.required_labels) and route is None:
                    continue
                selected.append((resource, route))
                if len(selected) == requirement.quantity:
                    break
            if len(selected) != requirement.quantity:
                return None
            for resource, route in selected:
                used.add(resource.resource_id.root)
                bindings.append(
                    PlanResourceBinding(
                        lease_id=stable_id(f"{plan_id.root}:lease:{resource.resource_id.root}"),
                        resource_id=resource.resource_id,
                        resource_type=resource.resource_type,
                        properties=dict(resource.properties),
                        labels=dict(resource.labels),
                        lease_revision=1,
                        expires_at=expires_at,
                        switch_route=route,
                    )
                )
        return tuple(bindings)

    @staticmethod
    def _resource_matches(resource: ResourceCapability, requirement: ResourceRequirement) -> bool:
        return (
            resource.health.value == "ready"
            and resource.resource_type == requirement.resource_type
            and (requirement.vendor is None or resource.vendor == requirement.vendor)
            and (requirement.model is None or resource.model == requirement.model)
            and all(resource.properties.get(key) == value for key, value in requirement.properties.items())
        )

    @staticmethod
    def _labels_match(actual: dict[str, str], required: dict[str, str]) -> bool:
        return all(actual.get(key) == value for key, value in required.items())

    @staticmethod
    def _preferred_label_score(actual: dict[str, str], preferred: dict[str, str]) -> int:
        return sum(1 for key, value in preferred.items() if actual.get(key) == value)

    @staticmethod
    def _switch_route(
        resource: ResourceCapability,
        requirement: ResourceRequirement,
    ) -> SwitchRouteAllocation | None:
        if not requirement.allow_switching or resource.switch_connection is None:
            return None
        if SchedulerService._labels_match(resource.labels, requirement.required_labels):
            return None
        for port in resource.switch_connection.ports:
            if SchedulerService._labels_match(port.labels, requirement.required_labels):
                from aetp_protocol.capabilities import SwitchRouteAllocation

                return SwitchRouteAllocation(
                    switch_device_id=resource.switch_connection.switch_device_id,
                    port=port.port,
                )
        return None

    def _plugin_package(self, uow: UnitOfWork, script: RunScriptSnapshot) -> PluginDistributionRef | None:
        record = uow.plugin_versions.get(script.executor.plugin_id, script.executor.version)
        if record is None or record.status is PluginStatus.REMOVED:
            return None
        if record.archive_sha256 != script.executor.archive_sha256:
            return None
        url = (
            self._plugin_url_builder(script.executor.plugin_id, script.executor.version)
            if self._plugin_url_builder is not None
            else None
        )
        return PluginDistributionRef(
            plugin_id=script.executor.plugin_id,
            version=script.executor.version,
            archive_sha256=script.executor.archive_sha256,
            download_url=url,
        )

    @staticmethod
    def _scripts_by_binding(snapshot: RunSnapshot) -> dict[str, RunScriptSnapshot]:
        return {script.binding_id.root: script for script in snapshot.scripts}

    @staticmethod
    def _eligible_bindings(snapshot: RunSnapshot, shards: list[RunShard]) -> set[str]:
        enabled = [script.binding_id.root for script in snapshot.scripts]
        if snapshot.execution_mode == "parallel":
            return set(enabled)
        for binding_id in enabled:
            binding_shards = [shard for shard in shards if shard.script_binding_id == binding_id]
            if any(shard.status not in _TERMINAL_SHARD_STATUSES for shard in binding_shards):
                return {binding_id}
        return set()

    @staticmethod
    def _is_dispatchable(uow: UnitOfWork, shard: RunShard, snapshot: RunSnapshot) -> bool:
        if shard.status not in {ShardStatus.PENDING, ShardStatus.WAITING_RECOVERY}:
            return shard.status is ShardStatus.DISPATCHING and SchedulerService._retry_available(uow, shard, snapshot)
        attempts = uow.shard_attempts.list_by_shard(shard.shard_id)
        if any(attempt.status in _ACTIVE_ATTEMPT_STATUSES for attempt in attempts):
            return False
        if any(attempt.status is ShardAttemptStatus.UNKNOWN for attempt in attempts):
            return False
        return SchedulerService._retry_available(uow, shard, snapshot)

    @staticmethod
    def _retry_available(uow: UnitOfWork, shard: RunShard, snapshot: RunSnapshot) -> bool:
        attempts = uow.shard_attempts.list_by_shard(shard.shard_id)
        latest = max(attempts, key=lambda attempt: attempt.attempt_no, default=None)
        if latest is None:
            return True
        if latest.status not in {
            ShardAttemptStatus.SUCCEEDED,
            ShardAttemptStatus.FAILED,
            ShardAttemptStatus.CANCELLED,
            ShardAttemptStatus.TIMED_OUT,
            ShardAttemptStatus.LOST,
        }:
            return False
        return latest.attempt_no < snapshot.retry_policy.max_attempts

    @staticmethod
    def _mark_exhausted_if_needed(uow: UnitOfWork, shard: RunShard, snapshot: RunSnapshot) -> bool:
        if shard.status is not ShardStatus.DISPATCHING:
            return False
        attempts = uow.shard_attempts.list_by_shard(shard.shard_id)
        latest = max(attempts, key=lambda attempt: attempt.attempt_no, default=None)
        if latest is None or latest.status not in {
            ShardAttemptStatus.FAILED,
            ShardAttemptStatus.TIMED_OUT,
        } or latest.attempt_no < snapshot.retry_policy.max_attempts:
            return False
        assert_transition(shard.status, ShardStatus.FAILED)
        shard.status = ShardStatus.FAILED
        shard.final_node = latest.node_id
        uow.run_shards.update(shard)
        return True

    @staticmethod
    def _cancel_after_failure(
        uow: UnitOfWork,
        shards: list[RunShard],
        snapshot: RunSnapshot,
    ) -> list[str]:
        cancelled: list[str] = []
        failed_seen = False
        ordered_bindings = [script.binding_id.root for script in snapshot.scripts]
        for binding_id in ordered_bindings:
            binding_shards = [shard for shard in shards if shard.script_binding_id == binding_id]
            if failed_seen:
                for shard in binding_shards:
                    if shard.status is ShardStatus.PENDING:
                        assert_transition(shard.status, ShardStatus.CANCELLED)
                        shard.status = ShardStatus.CANCELLED
                        uow.run_shards.update(shard)
                        cancelled.append(shard.shard_id)
                continue
            if any(shard.status in {ShardStatus.FAILED, ShardStatus.TIMED_OUT} for shard in binding_shards):
                failed_seen = True
        return cancelled

    @staticmethod
    def _project_cancelled_run_if_terminal(uow: UnitOfWork, run: TaskRun) -> None:
        if run.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
            RunStatus.LOST,
        }:
            return
        shards = uow.run_shards.list_by_run(run.run_id)
        if shards and all(shard.status in _TERMINAL_SHARD_STATUSES for shard in shards):
            target = RunStatus.SUCCEEDED
            if any(shard.status in {ShardStatus.FAILED, ShardStatus.TIMED_OUT} for shard in shards):
                target = RunStatus.FAILED
            elif any(shard.status is ShardStatus.CANCELLED for shard in shards):
                target = RunStatus.CANCELLED
            if run.status not in {RunStatus.CREATED, RunStatus.DISPATCHED, RunStatus.ACKED, RunStatus.RUNNING}:
                return
            assert_transition(run.status, target)
            run.status = target
            run.finished_at = utcnow()
            uow.task_runs.update(run)


_ACTIVE_ATTEMPT_STATUSES = {
    ShardAttemptStatus.CREATED,
    ShardAttemptStatus.DISPATCHED,
    ShardAttemptStatus.ACKED,
    ShardAttemptStatus.RUNNING,
}
_TERMINAL_SHARD_STATUSES = {
    ShardStatus.SUCCEEDED,
    ShardStatus.FAILED,
    ShardStatus.CANCELLED,
    ShardStatus.TIMED_OUT,
}


__all__ = ["ScheduledPlan", "ScheduleResult", "SchedulerService"]
