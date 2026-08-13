"""关键消息载荷 DTO（§8.4 + verify 扩展）。

Pydantic 模型，extra=forbid 拒绝非法字段；Master/Agent 共用同一契约。
本节只定义核心 payload；运行期日志/进度等按需后续补充。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeRegisterPayload(_Strict):
    """节点注册（§8.4：node_id、name、capabilities、tags、supported_versions、plugin_versions）。"""

    # sym:node_id 节点业务标识
    node_id: str
    # sym:name 节点名
    name: str = ""
    # sym:capabilities 硬件/能力谓词（硬件匹配 D-23 依据）
    capabilities: dict[str, Any] = Field(default_factory=dict)
    # sym:tags 节点标签（tag 匹配）
    tags: list[str] = Field(default_factory=list)
    # sym:supported_versions 插件兼容版本（按 task_type 分组）
    supported_versions: dict[str, list[str]] = Field(default_factory=dict)
    # sym:plugin_versions 已装插件版本（task_type -> version）
    plugin_versions: dict[str, str] = Field(default_factory=dict)


class NodeHeartbeatPayload(_Strict):
    """节点心跳（§8.4/§8.5：node_id、status、load、active_run_ids；只刷新投影）。"""

    # sym:node_id 节点业务标识
    node_id: str
    # sym:status 在线状态（online/offline）
    status: str = "online"
    # sym:load 负载（结构化：{running_shards, queued_shards}，§18.5 调度排序）
    load: dict[str, int] = Field(default_factory=dict)
    # sym:active_run_ids 当前活动 run_id 列表（离线恢复现场）
    active_run_ids: list[str] = Field(default_factory=list)


class ScriptVerifyPayload(_Strict):
    """脚本编译/格式验证命令（verify_location=agent，Master→Agent）。"""

    # sym:verify_id 验证请求 ID（verify-result 幂等键）
    verify_id: str
    # sym:script_id 脚本业务标识
    script_id: str
    # sym:version 脚本版本
    version: int
    # sym:task_type 任务类型（Agent 选插件）
    task_type: str
    # sym:script_ref 脚本引用 {script_id, version, sha256, download_url}
    script_ref: dict[str, Any] = Field(default_factory=dict)
    # sym:config 插件配置（验证输入）
    config: dict[str, Any] = Field(default_factory=dict)


class ScriptVerifyResultPayload(_Strict):
    """脚本验证结果回传（Agent→Master，按 verify_id 幂等）。"""

    # sym:verify_id 对应 ScriptVerifyPayload.verify_id
    verify_id: str
    # sym:script_id 脚本业务标识
    script_id: str
    # sym:errors 验证错误列表（空 = 通过）
    errors: list[str] = Field(default_factory=list)


class RunAssignPayload(_Strict):
    """Shard 派发（§8.4 run.assign；含 Shard 专属 execution_params）。"""

    # sym:project_id 项目业务标识（仅审计/日志归属）
    project_id: str
    # sym:task_id 任务定义业务标识
    task_id: str
    # sym:shard_id Shard 业务标识
    shard_id: str
    # sym:shard_index Run 内序号
    shard_index: int
    # sym:run_id Run 业务标识（Agent 以 (run_id, attempt_no) 原子 claim）
    run_id: str
    # sym:attempt_no 派发尝试序号（D-20 failover）
    attempt_no: int
    # sym:dispatch_id 本次投递意图 ID（无 ACK 重投保留）
    dispatch_id: str
    # sym:task_type 任务类型（Agent 查插件 registry）
    task_type: str
    # sym:plugin_version 插件版本（Agent 校验 PLUGIN_VERSION_MISMATCH）
    plugin_version: str
    # sym:script_ref 脚本引用 {script_id, version, sha256, download_url}
    script_ref: dict[str, Any]
    # sym:case_keys 该 Shard 负责的 case 集合
    case_keys: list[str] = Field(default_factory=list)
    # sym:execution_params 每 Shard 专属执行参数（插件 execute 使用）
    execution_params: dict[str, Any] = Field(default_factory=dict)
    # sym:timeout_s 任务超时（秒；0=不限制）
    timeout_s: int = 0


class RunAckPayload(_Strict):
    """命令 ACK（§8.4 run.ack；重复 ACK 幂等）。"""

    # sym:run_id 对应 Run
    run_id: str
    # sym:attempt_no 派发尝试序号
    attempt_no: int
    # sym:dispatch_id 对应派发 ID
    dispatch_id: str
    # sym:accepted 是否接受（false=拒绝，reason 说明）
    accepted: bool = True
    # sym:reason 拒绝/接受说明（含错误码）
    reason: str = ""


class RunResultPayload(_Strict):
    """最终结果（§8.4 run.result；一个 attempt 只接收一个最终结果，D-19）。"""

    # sym:run_id 对应 Run
    run_id: str
    # sym:attempt_no 派发尝试序号
    attempt_no: int
    # sym:status 结果状态（succeeded/failed/cancelled/timed_out）
    status: str
    # sym:passed 是否全部通过
    passed: bool = False
    # sym:case_results 结构化 case 结果列表（插件 parse_results 产出，D-19）
    case_results: list[dict[str, Any]] = Field(default_factory=list)
    # sym:metrics 汇总指标
    metrics: dict[str, Any] = Field(default_factory=dict)
    # sym:data 汇总数据
    data: dict[str, Any] = Field(default_factory=dict)
    # sym:artifact_refs 产物引用（报告/日志归档）
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    # sym:started_at 开始时间
    started_at: datetime | None = None
    # sym:finished_at 结束时间
    finished_at: datetime | None = None
