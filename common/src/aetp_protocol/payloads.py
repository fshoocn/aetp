"""关键消息载荷 DTO（§8.4 + verify 扩展）。

Pydantic 模型，extra=forbid 拒绝非法字段；Master/Agent 共用同一契约。
本节只定义核心 payload；运行期日志/进度等按需后续补充。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .capabilities import DeviceAllocation, NodeCapabilities


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeRegisterPayload(_Strict):
    """节点注册（§8.4：node_id、name、capabilities、tags、supported_versions、plugin_versions）。

        capabilities 为**强类型对象图**（`NodeCapabilities` 模型，§18.5/§8.4）：
                {
                    "vehicle": {"vendors": [{"name": "vector", "buses": [{
                        "bus_type": "can", "channels": [{"name": "can0", "enabled": true}]
                    }]}]},
                    "language": {"runtimes": [{"name": "python", "version": "3.11"}]},
                    "system": {"operating_system": {"name": "windows", "version": "10.0.19045"},
                                         "memory_mb": 16384, "cpu_cores": 8},
                    "serial": {"ports": [{"function": "psu", "port": "30", "enabled": true}]}
                }
        分类：vehicle 车载（厂商→总线→通道对象）、language 语言运行时、
        system 系统信息、serial 串口功能对象（功能→端口）。
    结构由 Pydantic 模型校验（extra=forbid 拒绝未知字段/类型错误），
    Agent 启动时从节点根目录能力配置文件读取并上报（P5）。
    """

    # sym:node_id 节点业务标识
    node_id: str
    # sym:name 节点名
    name: str = ""
    # sym:capabilities 层级能力树（强类型模型 NodeCapabilities）
    capabilities: NodeCapabilities = Field(default_factory=NodeCapabilities)
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


class RegisterAckPayload(_Strict):
    """节点注册回执（Master -> Agent）。"""

    # sym:node_id 回执对应的节点
    node_id: str
    # sym:session_id 回执对应的 Agent 会话
    session_id: str
    # sym:accepted 是否接受本次注册
    accepted: bool = True
    # sym:reason 拒绝原因（accepted=false 时使用）
    reason: str = ""


class PluginPackageRef(_Strict):
    """Agent 执行插件包引用（Master -> Agent）。"""

    # sym:task_type 插件任务类型
    task_type: str
    # sym:package_name 已安装 Python 包名或受信任插件包标识
    package_name: str
    # sym:version 本次 run 所需的插件版本
    version: str
    # sym:download_url Master 签发的限时下载地址
    download_url: str
    # sym:sha256 下载包完整内容 SHA-256
    sha256: str = Field(min_length=64, max_length=64)
    # sym:entry_point Agent 侧执行插件入口点（module:attribute）
    entry_point: str


class PresencePayload(_Strict):
    """节点非正常离线（LWT，§8.6：仅携带 node_id/reason/sent_at，不含实时任务快照）。

    LWT 在 CONNECT 时固定设置、Broker 不能动态替换 payload，因此不携带
    current_run_id；Master 收到后按 OfflinePolicy 处理（§8.6 步骤）。
    """

    # sym:node_id 节点业务标识
    node_id: str
    # sym:reason 断开原因（unexpected_disconnect 等，DisconnectReason）
    reason: str = "unexpected_disconnect"
    # sym:sent_at 离线时间（UTC）
    sent_at: datetime | None = None


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
    # sym:plugin_version 本次验证所需插件版本（Agent 校验兼容性）
    plugin_version: str = ""
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


class ScriptParsePayload(_Strict):
    """脚本辅助解析命令（parse_location=agent，Master→Agent，P5.7）。

    仅在插件声明需要台架环境才能解析/预检时由 Master 下发；常规脚本
    解析仍由 Master 面完成（D-17）。Agent 只回传解析出的原始用例事实，
    主用例索引（``script_cases``）最终仍由 Master 校验后生成。
    """

    # sym:parse_id 解析请求 ID（parse-result 幂等键）
    parse_id: str
    # sym:script_id 脚本业务标识
    script_id: str
    # sym:version 脚本版本
    version: int
    # sym:task_type 任务类型（Agent 选插件）
    task_type: str
    # sym:plugin_version 本次解析所需插件版本（Agent 校验兼容性）
    plugin_version: str = ""
    # sym:script_ref 脚本引用 {script_id, version, sha256, download_url}
    script_ref: dict[str, Any] = Field(default_factory=dict)
    # sym:config 插件配置（解析输入）
    config: dict[str, Any] = Field(default_factory=dict)


class ScriptParseResultPayload(_Strict):
    """脚本辅助解析结果回传（Agent→Master，按 parse_id 幂等，P5.7）。"""

    # sym:parse_id 对应 ScriptParsePayload.parse_id
    parse_id: str
    # sym:script_id 脚本业务标识
    script_id: str
    # sym:cases 解析出的用例事实列表（stable_key/name/parent_path/tags/params）
    cases: list[dict[str, Any]] = Field(default_factory=list)
    # sym:errors 解析错误列表（空 = 成功）
    errors: list[str] = Field(default_factory=list)


class RunAssignPayload(_Strict):
    """Shard 派发（§8.4 run.assign；Master 已预留具体设备）。"""

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
    # sym:device_allocations Master 已原子选择并预留的全部物理设备
    device_allocations: list[DeviceAllocation] = Field(default_factory=list)
    # sym:dispatch_id 本次投递意图 ID（无 ACK 重投保留）
    dispatch_id: str
    # sym:task_type 任务类型（Agent 查插件 registry）
    task_type: str
    # sym:plugin_version 插件版本（Agent 校验 PLUGIN_VERSION_MISMATCH）
    plugin_version: str
    # sym:plugin_ref Agent 缺失/版本不符时下载并安装的执行插件包
    plugin_ref: PluginPackageRef | None = None
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


class RunCancelPayload(_Strict):
    """任务取消（§8.4 run.cancel；Agent 设置取消标志，最终以 result 为准）。"""

    # sym:run_id 对应 Run
    run_id: str
    # sym:reason 取消原因
    reason: str = ""


class RunProgressPayload(_Strict):
    """进度上报（§8.4 run.progress；QoS0 可丢，sequence 单调递增）。"""

    # sym:run_id 对应 Run
    run_id: str
    # sym:sequence 进度序号（ge=1）
    sequence: int = Field(ge=1)
    # sym:percent 完成百分比（0..100）
    percent: int = Field(ge=0, le=100)
    # sym:stage 当前阶段名
    stage: str = ""
    # sym:message 进度说明
    message: str = ""


class RunCaseStatusPayload(_Strict):
    """case 级状态上报（§8.4 run.case-status；仅支持实时 case 结果的插件）。"""

    # sym:run_id 对应 Run
    run_id: str
    # sym:case_key 用例稳定键
    case_key: str
    # sym:status 状态（pending/running/passed/failed/skipped/error）
    status: str


class CaseResultEntry(_Strict):
    """结构化 case 级结果（§8.4 run.result.case_results，D-19）。

    CANoe 类插件运行中无 case 级实时数据，结束后由 Agent 面 ``analyze_results``
    分析报告产出；pytest 类也可实时上报，但最终结果仍以本结构随 ``run.result``
    一次性上报。``case_key`` 与脚本用例索引的 ``stable_key`` 一致。
    """

    # sym:case_key 用例稳定键（= script_cases.stable_key）
    case_key: str = Field(min_length=1, max_length=256)
    # sym:status 结果状态（passed/failed/skipped/error）
    status: str = Field(pattern=r"^(passed|failed|skipped|error)$")
    # sym:duration_ms 执行耗时（毫秒；仅成功统计 avg_duration_s 数据源，D-21）
    duration_ms: int | None = Field(default=None, ge=0)
    # sym:error_summary 失败/错误摘要
    error_summary: str | None = None
    # sym:detail 结构化详情（断言信息、堆栈等）
    detail: dict[str, Any] | None = None


class RunResultPayload(_Strict):
    """最终结果（§8.4 run.result；一个 attempt 只接收一个最终结果，D-19）。

    ``shard_id`` + ``attempt_no`` 唯一锁定一个 Attempt：attempt_no 是
    Shard 内序号（多 Shard 各自独立递增），仅靠 run_id + attempt_no 无法
    唯一定位，必须携带 shard_id。
    """

    # sym:run_id 对应 Run
    run_id: str
    # sym:shard_id 所属 Shard（与 attempt_no 共同锁定 Attempt）
    shard_id: str
    # sym:attempt_no 派发尝试序号
    attempt_no: int
    # sym:status 结果状态（succeeded/failed/cancelled/timed_out）
    status: str
    # sym:passed 是否全部通过
    passed: bool = False
    # sym:case_results 结构化 case 结果列表（插件 analyze_results 产出，D-19）
    case_results: list[CaseResultEntry] = Field(default_factory=list)
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


class RunLogCompletePayload(_Strict):
    """日志围栏（§8.4 run.log-complete；P6.6）。

    Agent 在执行结束、flush 完剩余 spool 日志后发布；Master 以此为日志
    围栏：此后拒绝该 run 的任何日志条目并触发 ``run.log_complete`` SSE。
    """

    # sym:run_id 对应 Run
    run_id: str
    # sym:last_sequence 末条日志 sequence（ge=0，0 表示无日志）
    last_sequence: int = Field(ge=0)
    # sym:entry_count 日志总条数
    entry_count: int = Field(ge=0)
    # sym:artifact_refs 产物引用（报告/日志归档，随围栏一并声明）
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
