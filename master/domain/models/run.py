"""领域对象：Run 执行域（P3.4）。

一次 Run 是任务定义的一次真实执行快照：script_ref / case_selection /
split_policy 均在 Run 创建时固化（触发时 case_filter 可变，Shard 因此是
Run 级，§18.6）。Shard 由插件 split_shards 分割产出；Attempt 是 Shard 向
某 Node 的一次派发尝试，历史失败全量保留（D-20，不得覆盖）。
results 为 Run 级汇总投影（result_id；run_id 唯一）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from master.domain.enums import (
    ArtifactKind,
    CaseStatus,
    RunStatus,
    ShardAttemptStatus,
    ShardStatus,
    TriggerType,
)
from master.domain.time import utcnow


@dataclass
class TaskRun:
    """一次 Run 执行（task_runs 表）。

    script_ref 为 {script_id, version, sha256} 快照——即使脚本后续升级，
    本次 Run 仍用创建时引用的脚本版本。
    """

    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:run_id Run 业务标识（ULID），全局唯一，对外暴露
    run_id: str = ""
    # sym:project_id 所属项目业务标识（Run 的 project 必须与任务定义一致）
    project_id: str = ""
    # sym:task_id 引用的任务定义业务标识（不复制定义，只引用）
    task_id: str = ""
    # sym:script_ref 脚本引用快照 {script_id, version, sha256}（§7.5）
    script_ref: dict = field(default_factory=dict)
    # sym:case_selection 本次 Run 生效的 case 集合（默认集合或 case_filter 覆盖，D-15）
    case_selection: list[str] = field(default_factory=list)
    # sym:split_policy 本次 Run 的分割策略快照（§18.6）
    split_policy: dict = field(default_factory=dict)
    # sym:trigger_type 触发来源（manual_web/api/schedule/ci_webhook/retry/recovery，§18.7）
    trigger_type: TriggerType = TriggerType.MANUAL_WEB
    # sym:triggered_by_user_id 触发用户（系统触发如 schedule/retry 时为空）
    triggered_by_user_id: int | None = None
    # sym:integration_id 触发来源 CI 集成标识（非 CI 触发为空）
    integration_id: str | None = None
    # sym:trigger_context 触发上下文（如 retry 引用原 run_id、webhook 事件等）
    trigger_context: dict | None = None
    # sym:status Run 总体状态（created/dispatched/acked/running/...，§6.4）
    status: RunStatus = RunStatus.CREATED
    # sym:started_at 首次进入 running 的时间
    started_at: datetime | None = None
    # sym:finished_at 进入终态的时间
    finished_at: datetime | None = None
    # sym:log_complete 日志围栏：Agent 发布 run.log-complete 后置位，
    #   Master 此后拒绝该 run 的任何日志条目（P6.6）
    log_complete: bool = False
    # sym:last_log_sequence 围栏时记录的末条日志 sequence
    last_log_sequence: int | None = None
    # sym:created_at 创建时间（UTC）
    created_at: datetime = field(default_factory=utcnow)
    # sym:updated_at 最后更新时间（UTC）
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class RunShard:
    """Run 内的一个 Shard（run_shards 表）。

    由插件 split_shards 在 Run 创建时分割产出；case_keys 为本次 Run
    内该 Shard 负责的 case 集合。
    """

    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:shard_id Shard 业务标识（ULID），全局唯一
    shard_id: str = ""
    # sym:run_id 所属 Run 业务标识
    run_id: str = ""
    # sym:shard_index Run 内序号；(run_pk, shard_index) 唯一
    shard_index: int = 0
    # sym:case_keys 该 Shard 负责的 case 集合（stable_key 列表）
    case_keys: list[str] = field(default_factory=list)
    # sym:execution_params 该子任务专属执行参数（插件 split_shards 产出，
    #   执行时与共享 config 合并/覆盖；run.assign 下发，Agent 插件 execute 使用）
    execution_params: dict = field(default_factory=dict)
    # sym:estimated_duration_s 预估耗时（秒，by_time 分割产出；null=未知）
    estimated_duration_s: float | None = None
    # sym:status Shard 状态（pending/dispatching/running/...，§5.4）
    status: ShardStatus = ShardStatus.PENDING
    # sym:final_node 最终执行节点业务 ID（多 attempt 后取最后有效者）
    final_node: str | None = None
    # sym:created_at 创建时间（UTC）
    created_at: datetime = field(default_factory=utcnow)
    # sym:updated_at 最后更新时间（UTC）
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class ShardAttempt:
    """Shard 向某 Node 的一次派发尝试（shard_attempts 表）。

    (shard_pk, attempt_no) 唯一；换节点 failover 递增 attempt_no，
    历史失败全量保留（D-20）。
    """

    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:attempt_id Attempt 业务标识（ULID），全局唯一
    attempt_id: str = ""
    # sym:shard_id 所属 Shard 业务标识
    shard_id: str = ""
    # sym:attempt_no 尝试序号（自 1 递增）；(shard_pk, attempt_no) 唯一
    attempt_no: int = 1
    # sym:node_id 执行节点业务 ID（failover 换节点时变化）
    node_id: str = ""
    # sym:device_ids 本次 Attempt 原子占用的全部物理设备；历史记录可为空
    device_ids: list[str] = field(default_factory=list)
    # sym:status 尝试状态（created/dispatched/acked/running/...）
    status: ShardAttemptStatus = ShardAttemptStatus.CREATED
    # sym:error_code 领域错误码（如 NODE_CAPABILITY_MISMATCH）
    error_code: str | None = None
    # sym:error_message 失败描述（历史失败信息全量保留，D-20）
    error_message: str | None = None
    # sym:started_at 开始执行时间
    started_at: datetime | None = None
    # sym:finished_at 结束时间
    finished_at: datetime | None = None
    # sym:created_at 创建时间（UTC）
    created_at: datetime = field(default_factory=utcnow)
    # sym:updated_at 最后更新时间（UTC）
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class RunCaseResult:
    """case 级执行结果（run_case_results 表）。

    (run_pk, shard_pk, case_key, attempt_no) 唯一；按 attempt 全量保留（D-20），
    历史失败不被后续成功覆盖。
    """

    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:run_id 所属 Run 业务标识
    run_id: str = ""
    # sym:shard_id 所属 Shard 业务标识
    shard_id: str = ""
    # sym:case_key 用例稳定标识（stable_key）
    case_key: str = ""
    # sym:attempt_no 关联的派发尝试序号
    attempt_no: int = 1
    # sym:status case 结果状态（passed/failed/skipped/error/...）
    status: CaseStatus = CaseStatus.PENDING
    # sym:duration_ms 执行耗时（毫秒；仅成功统计 avg_duration_s 数据源，D-21）
    duration_ms: int | None = None
    # sym:error_summary 失败/错误摘要
    error_summary: str | None = None
    # sym:detail 结构化详情（如断言信息、堆栈），插件上报
    detail: dict | None = None
    # sym:created_at 创建时间（UTC）
    created_at: datetime = field(default_factory=utcnow)
    # sym:updated_at 最后更新时间（UTC）
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class RunArtifact:
    """结束产物引用（run_artifacts 表）。

    文件存 data/artifacts/{run_id}/，DB 只存引用与 sha256（§6.2）。
    """

    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:artifact_id 产物业务标识（ULID），全局唯一
    artifact_id: str = ""
    # sym:run_id 所属 Run 业务标识
    run_id: str = ""
    # sym:shard_id 所属 Shard 业务标识（Run 级产物为空）
    shard_id: str | None = None
    # sym:node_id 上传节点业务 ID
    node_id: str | None = None
    # sym:kind 产物类型（report/log_archive/data）
    kind: ArtifactKind = ArtifactKind.REPORT
    # sym:file_ref 文件引用路径（data/artifacts/{run_id}/...）
    file_ref: str = ""
    # sym:size 文件字节数
    size: int = 0
    # sym:sha256 内容哈希（下载校验）
    sha256: str = ""
    # sym:uploaded_at 上传时间（UTC）
    uploaded_at: datetime = field(default_factory=utcnow)
    # sym:created_at 记录创建时间（UTC）
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class RunResult:
    """Run 级汇总投影（results 表）。

    run_id 唯一；由各 Shard/Attempt/case 结果投影计算（§5.4 规则 4），
    不重复存 Shard 明细。
    """

    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:result_id 汇总投影业务标识（ULID），全局唯一
    result_id: str = ""
    # sym:run_id 对应 Run 业务标识（唯一，一 Run 一行投影）
    run_id: str = ""
    # sym:project_id 所属项目业务标识
    project_id: str = ""
    # sym:task_id 任务定义业务标识
    task_id: str = ""
    # sym:node_id 最终执行节点业务 ID（单节点场景；多 Shard 可为空）
    node_id: str | None = None
    # sym:passed 是否全部通过（供列表快速展示）
    passed: bool = False
    # sym:status Run 总体状态
    status: RunStatus = RunStatus.CREATED
    # sym:metrics 汇总指标 JSON（如总耗时、通过/失败数）
    metrics: dict | None = None
    # sym:data 汇总数据 JSON
    data: dict | None = None
    # sym:started_at 开始时间（UTC）
    started_at: datetime | None = None
    # sym:finished_at 结束时间（UTC）
    finished_at: datetime | None = None
    # sym:created_at 创建时间（UTC）
    created_at: datetime = field(default_factory=utcnow)
    # sym:updated_at 最后更新时间（UTC）
    updated_at: datetime = field(default_factory=utcnow)


# 防止 pytest 将 Run 执行域类误识别为测试类（测试文件中会导入本模块）。
TaskRun.__test__ = False
RunShard.__test__ = False
ShardAttempt.__test__ = False
RunCaseResult.__test__ = False
RunArtifact.__test__ = False
RunResult.__test__ = False
