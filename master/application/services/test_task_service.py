"""测试任务定义服务（P4.5 延伸，§18.4/§18.5 D-23 节点筛选；P7.4 CRUD）。

创建/编辑任务定义时的节点筛选（保存时校验）：
1. 第一层硬校验：node_ids ⊆ 项目绑定节点，否则 PROJECT_ACCESS_DENIED（D-23）。
2. 从引用脚本读 hardware_requirements（插件上传时生成，§18.5）。
3. 对每个选中节点做能力匹配（CapabilityService.evaluate）。
4. 软校验：无任何节点满足 → 返回 warning（可保存，仅告警，§18.5）；
   触发 Run 时由调度器（P4.6）做第二层硬校验（NODE_CAPABILITY_MISMATCH）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from aetp_protocol.ids import new_id

from master.application.errors import (
    ProjectAccessDeniedError,
    ScriptNotFoundError,
    TaskNotFoundError,
)
from master.application.services.capability_service import CapabilityService
from master.domain.enums import ScriptParseStatus
from master.domain.models import TestTask
from master.domain.repositories import UnitOfWork

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NodeCandidateMatch:
    """单个候选节点的匹配结果。"""

    # sym:node_id 节点业务标识
    node_id: str
    # sym:matched 是否满足硬件需求
    matched: bool
    # sym:failures 不满足原因（匹配成功为空）
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeSelectionResult:
    """创建任务定义时的节点筛选结果（软校验输出）。"""

    # sym:node_ids 校验过的节点（⊆ 项目绑定）
    node_ids: tuple[str, ...] = ()
    # sym:matches 每个节点的匹配明细
    matches: tuple[NodeCandidateMatch, ...] = ()
    # sym:matched_count 满足硬件需求的节点数
    matched_count: int = 0
    # sym:warning 软校验告警（无满足节点时非空；不阻止保存）
    warning: str | None = None


class TestTaskService:
    """测试任务定义节点筛选（任务定义 CRUD 的其余部分属 P7.4）。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        capability_service: CapabilityService,
    ) -> None:
        self._uow_factory = uow_factory
        self._capability = capability_service

    def validate_node_selection(
        self,
        project_id: str,
        node_ids: list[str] | tuple[str, ...],
        script_id: str,
    ) -> NodeSelectionResult:
        """创建/编辑任务定义时的节点筛选（§18.5 保存时校验，D-23）。

        第一层硬校验（非项目绑定 → PROJECT_ACCESS_DENIED）+ 硬件需求软校验
        （无满足节点仅告警，不阻止保存）。
        """
        node_ids = list(node_ids)

        with self._uow_factory() as uow:
            # 引用脚本必须存在且属于当前项目（IDOR 防护）
            script = uow.test_scripts.get_by_script_id(script_id)
            if script is None or script.project_id != project_id:
                raise ScriptNotFoundError(
                    f"脚本不存在或不属于当前项目: {script_id}"
                )

            # 第一层硬校验：node_ids ⊆ 项目绑定节点（D-23）
            bound = {b.node_id for b in uow.bindings.list_with_nodes(project_id)}
            invalid = [n for n in node_ids if n not in bound]
            if invalid:
                raise ProjectAccessDeniedError(
                    f"节点不在项目绑定范围（D-23）: {', '.join(sorted(invalid))}"
                )

            # 硬件需求软校验
            requirements = script.hardware_requirements
            matches: list[NodeCandidateMatch] = []
            for node_id in node_ids:
                node = uow.nodes.get_by_id(node_id)
                if node is None:
                    matches.append(
                        NodeCandidateMatch(
                            node_id=node_id, matched=False, failures=("节点不存在",)
                        )
                    )
                    continue
                result = self._capability.evaluate(node, requirements)
                matches.append(
                    NodeCandidateMatch(
                        node_id=node_id,
                        matched=result.matched,
                        failures=result.failures,
                    )
                )

        matched_count = sum(1 for m in matches if m.matched)
        warning: str | None = None
        if node_ids and matched_count == 0:
            warning = (
                "所选节点均不满足脚本硬件要求（可保存；触发 Run 时将硬校验 "
                "NODE_CAPABILITY_MISMATCH）"
            )
        logger.info(
            "任务定义节点筛选: project=%s nodes=%d matched=%d warning=%s",
            project_id,
            len(node_ids),
            matched_count,
            warning is not None,
        )
        return NodeSelectionResult(
            node_ids=tuple(node_ids),
            matches=tuple(matches),
            matched_count=matched_count,
            warning=warning,
        )

    # -- P7.4 任务定义 CRUD ---------------------------------------------------

    def create_task(
        self,
        *,
        project_id: str,
        name: str,
        script_id: str,
        default_case_selection: list[str] | None = None,
        node_ids: list[str] | None = None,
        split_policy: dict | None = None,
        retry_policy: dict | None = None,
        timeout_s: int = 0,
        priority: int = 0,
        created_by: int,
    ) -> TestTask:
        """创建任务定义（§18.4：脚本已解析、case 存在、节点 ⊆ 项目绑定）。"""
        selected_nodes = list(node_ids or [])
        self.validate_node_selection(project_id, selected_nodes, script_id)

        with self._uow_factory() as uow:
            script = uow.test_scripts.get_by_script_id(script_id)
            if script is None or script.project_id != project_id:
                raise ScriptNotFoundError(
                    f"脚本不存在或不属于当前项目: {script_id}"
                )
            if script.parse_status != ScriptParseStatus.PARSED:
                raise ValueError("脚本尚未解析完成，无法创建任务定义")
            if uow.test_tasks.find_by_name(project_id, name) is not None:
                raise ValueError(f"任务定义已存在: {name}")

            cases = uow.script_cases.list_by_script(script_id)
            selected = self._normalize_case_selection(
                default_case_selection, cases
            )
            normalized_split = self._normalize_split_policy(
                split_policy, cases, selected
            )
            normalized_retry = self._normalize_retry_policy(retry_policy)

            task = uow.test_tasks.add(
                TestTask(
                    task_id=new_id(),
                    project_id=project_id,
                    script_id=script_id,
                    script_version=script.version,
                    task_type=script.task_type,
                    name=name,
                    default_case_selection=selected,
                    node_ids=selected_nodes,
                    split_policy=normalized_split,
                    retry_policy=normalized_retry,
                    timeout_s=timeout_s,
                    priority=priority,
                    enabled=True,
                    created_by=created_by,
                )
            )
        logger.info("任务定义已创建: task_id=%s name=%s", task.task_id, name)
        return task

    def update_task(
        self,
        task_id: str,
        *,
        project_id: str,
        name: str | None = None,
        script_id: str | None = None,
        default_case_selection: list[str] | None = None,
        node_ids: list[str] | None = None,
        split_policy: dict | None = None,
        retry_policy: dict | None = None,
        timeout_s: int | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
    ) -> TestTask:
        """更新任务定义（全量字段，缺失保持原值）。"""
        with self._uow_factory() as uow:
            task = uow.test_tasks.get_by_task_id(task_id, project_id)
            if task is None:
                raise TaskNotFoundError(f"任务定义不存在: {task_id}")

            target_script_id = script_id or task.script_id
            script = uow.test_scripts.get_by_script_id(target_script_id)
            if script is None or script.project_id != project_id:
                raise ScriptNotFoundError(
                    f"脚本不存在或不属于当前项目: {target_script_id}"
                )
            if script.parse_status != ScriptParseStatus.PARSED:
                raise ValueError("脚本尚未解析完成，无法切换")
            cases = uow.script_cases.list_by_script(target_script_id)
            target_selection = (
                default_case_selection
                if default_case_selection is not None
                else task.default_case_selection
            )
            normalized_selection = self._normalize_case_selection(
                target_selection, cases
            )
            normalized_split = self._normalize_split_policy(
                split_policy if split_policy is not None else task.split_policy,
                cases,
                normalized_selection,
            )
            normalized_retry = self._normalize_retry_policy(
                retry_policy if retry_policy is not None else task.retry_policy
            )
            target_nodes = list(node_ids) if node_ids is not None else list(task.node_ids)
            self.validate_node_selection(project_id, target_nodes, target_script_id)

            if name is not None and name != task.name:
                existing = uow.test_tasks.find_by_name(project_id, name)
                if existing is not None and existing.task_id != task.task_id:
                    raise ValueError(f"任务定义已存在: {name}")

            # 脚本、用例、策略和节点的最终状态已在当前事务中确定；
            # 节点能力匹配已在写入前复用统一校验入口，避免保存绕过 D-23。
            script_changed = target_script_id != task.script_id
            target_script_id = script.script_id
            task.script_id = target_script_id
            if script_changed:
                task.script_version = script.version
                task.task_type = script.task_type

            if name is not None:
                task.name = name
            task.default_case_selection = normalized_selection
            task.node_ids = target_nodes
            task.split_policy = normalized_split
            task.retry_policy = normalized_retry
            if timeout_s is not None:
                task.timeout_s = timeout_s
            if enabled is not None:
                task.enabled = enabled
            if priority is not None:
                task.priority = priority

            task = uow.test_tasks.update(task)
        return task

    def get_task(self, task_id: str, project_id: str) -> TestTask | None:
        """查询任务定义（项目范围）。"""
        with self._uow_factory() as uow:
            return uow.test_tasks.get_by_task_id(task_id, project_id)

    def list_tasks(
        self, project_id: str, *, enabled: bool | None = None
    ) -> list[TestTask]:
        """列出项目任务定义。"""
        with self._uow_factory() as uow:
            return uow.test_tasks.list_by_project(project_id, enabled=enabled)

    def delete_task(self, task_id: str, project_id: str) -> None:
        """删除任务定义（硬删除）。

        历史 Run 的 task_pk 置空（Run 依赖自身快照 script_ref/case_selection/
        split_policy 仍可展示），随后删除任务定义并级联清理 schedule/bindings。
        """
        with self._uow_factory() as uow:
            task = uow.test_tasks.get_by_task_id(task_id, project_id)
            if task is None:
                raise TaskNotFoundError(f"任务定义不存在: {task_id}")
            if task.id is None:
                raise TaskNotFoundError(f"任务定义不存在: {task_id}")
            # 解除历史 Run 及 Run 汇总投影的任务引用（保留执行历史）
            detached_runs = uow.task_runs.nullify_task_for_runs(task_id)
            detached_results = uow.run_results.nullify_task_for_results(task_id)
            if detached_runs or detached_results:
                logger.info(
                    "任务删除：解除引用 task_id=%s runs=%d results=%d",
                    task_id,
                    detached_runs,
                    detached_results,
                )
            uow.test_tasks.delete(task.id)
            logger.info("任务定义硬删除: task_id=%s", task_id)

    @staticmethod
    def _normalize_case_selection(selection, cases) -> list[str]:
        """校验 stable_key；空集合表示运行该脚本的全部用例。"""
        requested = list(dict.fromkeys(selection or []))
        available = {case.stable_key for case in cases}
        invalid = [key for key in requested if key not in available]
        if invalid:
            raise ValueError(
                f"勾选的用例不存在于脚本索引: {', '.join(invalid[:5])}"
            )
        return requested

    @staticmethod
    def _normalize_split_policy(policy: dict | None, cases, selected: list[str]) -> dict:
        value = dict(policy or {})
        split_type = value.get("type", "none")
        if split_type == "none":
            return {"type": "none"}
        if split_type == "custom":
            return value
        if split_type == "by_case_count":
            count = value.get("cases_per_shard")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("by_case_count 的 cases_per_shard 必须是大于 0 的整数")
            return {"type": split_type, "cases_per_shard": count}
        if split_type == "by_time":
            target = value.get("target_duration_s")
            if isinstance(target, bool) or not isinstance(target, (int, float)) or target <= 0:
                raise ValueError("by_time 的 target_duration_s 必须大于 0")
            selected_set = set(selected)
            considered = (
                [case for case in cases if case.stable_key in selected_set]
                if selected_set
                else list(cases)
            )
            if not any(case.avg_duration_s is not None for case in considered):
                raise ValueError("全部用例无耗时数据，无法使用按时间分割（D-21）")
            return {"type": split_type, "target_duration_s": target}
        raise ValueError(f"不支持的分割策略: {split_type}")

    @staticmethod
    def _normalize_retry_policy(policy: dict | None) -> dict:
        value = dict(policy or {})
        max_attempts = value.get("max_attempts", 1)
        case_retry = value.get("case_retry", 0)
        failover_nodes = value.get("failover_nodes", False)
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 10
        ):
            raise ValueError("retry_policy.max_attempts 必须是 1 到 10 的整数")
        if isinstance(case_retry, bool) or not isinstance(case_retry, int) or not 0 <= case_retry <= 10:
            raise ValueError("retry_policy.case_retry 必须是 0 到 10 的整数")
        if not isinstance(failover_nodes, bool):
            raise ValueError("retry_policy.failover_nodes 必须是布尔值")
        return {
            "max_attempts": max_attempts,
            "failover_nodes": failover_nodes,
            "case_retry": case_retry,
        }
