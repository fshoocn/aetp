"""任务类型插件数据接口（P3.8，D-19）。

插件是随 Master/Agent 分发安装的 Python 包。Master 只通过本接口
拿/收三类数据：case 列表、Shard 分割结果、结构化 case 结果；
分割逻辑与报告解析全部在插件内部实现（§18.2）。

**插件与 Master 仅数据耦合**：本模块只依赖标准库与领域 DTO，
插件不得 import MQTT / FastAPI / 数据库实现或具体 broker 主题（§9.5）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from aetp_protocol.capabilities import HardwareRequirements


@dataclass(frozen=True)
class CaseInfo:
    """插件 parse_cases 产出的用例（写入 script_cases 的数据基础）。"""

    # sym:stable_key 跨版本稳定标识（pytest nodeid / CANoe 用例名 / cdd 路径），
    #   版本 diff 与任务定义勾选引用依赖它（§18.3）
    stable_key: str
    # sym:name 用例显示名
    name: str
    # sym:parent_path 父路径/分组（树形展示）
    parent_path: str = ""
    # sym:tags 用例标签
    tags: tuple[str, ...] = ()
    # sym:params 用例参数（插件执行时使用）
    params: Mapping[str, Any] = field(default_factory=dict)
    # sym:estimated_duration_s 预估耗时（秒；可选，D-21 缺耗时默认值数据源）
    estimated_duration_s: float | None = None


@dataclass(frozen=True)
class ShardSpec:
    """插件 split_shards 产出的一个**子任务（Shard）定义**（写入 run_shards）。

    Shard 是插件要执行的最小派发单元：Master 只负责派发/调度，不执行；
    Agent 侧插件以 execute(shard_context) 执行（§9.5/§18.2）。
    因此每个 Shard 自包含执行所需信息：case_keys + execution_params
    （每 Shard 专属执行参数，如 CAN 通道、测试参数）。物理资源由
    Master 调度时根据 HardwareRequirements 原子分配。
    """

    # sym:case_keys 该子任务负责的 case 集合（stable_key）
    case_keys: tuple[str, ...]
    # sym:execution_params 该子任务专属执行参数/指令（插件执行时使用；
    #   如 {"channel": 0} / {"config_section": "bench2"}，与共享 config 合并或覆盖）
    execution_params: Mapping[str, Any] = field(default_factory=dict)
    # sym:estimated_duration_s 预估耗时（秒；by_time 分割产出）
    estimated_duration_s: float | None = None


@dataclass(frozen=True)
class CaseResult:
    """插件 parse_results 产出的结构化 case 结果（写入 run_case_results，D-20）。"""

    # sym:case_key 用例稳定标识（对应 run_case_results.case_key）
    case_key: str
    # sym:status case 结果状态（CaseStatus 值：passed/failed/skipped/error/...）
    status: str
    # sym:duration_ms 执行耗时（毫秒；D-21 成功耗时统计数据源）
    duration_ms: int | None = None
    # sym:error_summary 失败/错误摘要
    error_summary: str | None = None
    # sym:detail 结构化详情（断言信息、堆栈等）
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskContext:
    """解析/执行上下文（§9.5 TaskContext 的数据子集）。

    P3.8 为数据接口，仅提供只读上下文；Agent 侧执行阶段另有
    progress/log/is_cancelled 等能力（P5/P6 补充）。
    """

    # sym:task_id 任务定义业务标识
    task_id: str
    # sym:shard_id Shard 业务标识
    shard_id: str
    # sym:run_id Run 业务标识
    run_id: str
    # sym:node_id 执行节点业务 ID
    node_id: str
    # sym:params 插件参数（config）
    params: Mapping[str, Any]
    # sym:script_ref 脚本引用快照 {script_id, version, sha256}（parse_results 定位报告）
    script_ref: Mapping[str, Any] = field(default_factory=dict)


class TaskTypePlugin(Protocol):
    """Master 侧任务类型插件（纯数据接口，§18.2）。

    具体插件（pytest/cdd/canoe）随包发布，由 bootstrap 容器显式注册到
    PluginRegistry；不得由项目配置上传/下载任意解析代码（§10.4）。
    """

    # sym:task_type 任务类型标识（全局唯一，注册表键）
    task_type: str
    # sym:display_name 展示名（API/前端任务类型清单）
    display_name: str
    # sym:plugin_version 插件版本（Master 校验兼容性）
    plugin_version: str
    # sym:supported_versions 兼容的插件版本集合（旧脚本迁移校验，§18.2）
    supported_versions: frozenset[str]
    # sym:config_schema 配置 Schema（通用表单兜底，D-16）
    config_schema: Mapping[str, Any]
    # sym:upload_spec 上传规格（文件类型白名单/大小上限）
    upload_spec: Mapping[str, Any]
    # sym:parse_location 用例解析位置（master/agent，D-17）
    parse_location: str
    # sym:result_parse_location 报告解析位置（master/agent，D-19）
    result_parse_location: str
    # sym:verify_location 脚本编译/格式验证执行位置（master/agent，与 D-17 同构）：
    #   pytest/cdd 可 master 端（py_compile/格式检查）；
    #   CANoe 工程验证依赖 CANoe COM → agent 端（下发 script.verify 到有验证能力的 Agent）
    verify_location: str

    def verify_script(
        self, script_dir: str, config: Mapping[str, Any]
    ) -> list[str]:
        """脚本编译/格式验证（上传后、解析前调用，快速失败）。

        插件最懂自身脚本格式：
        - pytest：py_compile 语法检查 + collect 兼容预检
        - cdd：XML/格式验证
        - CANoe：工程结构完整性（auto_create/template_upload/complete_upload 三模式）

        返回错误列表（空 = 通过）；有错则脚本 parse_status=failed，
        不进入解析队列（§18.3 步骤 2 前拦截）。
        按 verify_location 执行：master 端 Master 直接调用；
        agent 端 Master 下发 script.verify 到有验证能力的 Agent，由
        Agent 侧 ExecutionPlugin.verify_script 执行后回传结果。
        """
        ...

    async def parse_cases(
        self, script_dir: str, config: Mapping[str, Any]
    ) -> list[CaseInfo]:
        """用例解析：返回 CaseInfo 列表；stable_key 必须稳定（§18.3）。"""
        ...

    async def split_shards(
        self,
        cases: list[CaseInfo],
        policy: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> list[ShardSpec]:
        """任务分割：by_time（按耗时切到目标时长）/ by_case_count / custom（§18.6）。"""
        ...

    async def parse_results(
        self,
        artifact_files: list[str],
        context: TaskContext,
    ) -> list[CaseResult]:
        """测试报告解析为结构化 case 结果（D-19；CANoe 类仅结束后产生）。"""
        ...

    def hardware_requirements(
        self, config: Mapping[str, Any], cases: list[CaseInfo]
    ) -> HardwareRequirements:
        """强类型硬件需求（§18.5 节点匹配依据）。"""
        ...
