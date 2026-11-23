"""test_core 公共契约层：跨插件共用的纯数据结构与常量。

定位（``master_plugins/test_core/DESIGN.md`` §3）：与插件无关的提交面 /
下行面 / 上行面数据结构、协议常量、状态字面量与服务门面接口，供 test_core
核心、slave 执行器、消费插件三方共用。铁律：本模块**只用 stdlib**
（dataclasses / typing / collections.abc），不 import cordis、不 import db、
不 import 任何插件包——插件之间一切交互经 cordis，共享契约只住 ``common/``。

兼容规则（DESIGN §3.2，Phase 1 即生效）：

* ``JobDescriptor.proto`` / ``JobResult.proto`` 从 ``TEST_PROTO``（=1）起步，
  执行器不认识即拒收（§8.3）；
* 结构演进沿用心跳 config 先例：**忽略未知字段、缺省给默认值**；破坏性变更
  升 ``proto``，双版本并行一个过渡期；
* 事件载荷带 ``schema_version``（= ``SCHEMA_VERSION``），同规则演进。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# -- 协议与 schema 版本 ------------------------------------------------------

#: 下行 JobDescriptor / 上行 JobResult 的协议版本（§8.3 版本握手）
TEST_PROTO = 1
#: 事件载荷 schema 版本（载荷里以 ``schema_version`` 字段携带）
SCHEMA_VERSION = 1

# -- 通信 topic（§7，全部经 ctx.comm） ---------------------------------------

#: master → slave：派发子运行（载荷 JobDescriptor）
TOPIC_JOB = "test.job"
#: master → slave：取消（载荷 {job_id, attempt, reason}）
TOPIC_JOB_CANCEL = "test.job.cancel"
#: slave → master：结果上报（载荷 JobResult）
TOPIC_JOB_RESULT = "test.job.result"
#: slave → master：进度（可选，丢了无害）
TOPIC_JOB_PROGRESS = "test.job.progress"
#: slave → master：测试输出流小包（fire-and-forget，载荷 LogChunk，§10.4）
TOPIC_JOB_LOG = "test.job.log"

# -- 事件名（§11.1，登记于 common/event_contract.py，模式全 parallel） -------

#: 任务创建广播（submit 落库后，载荷 = 引用 + 摘要）
EVENT_TASK_CREATED = "test/task-created"
#: 任务状态迁移（载荷 {task_id, from, to, reason}）
EVENT_TASK_STATE = "test/task-state"
#: Job 终结（载荷含 attempt / 状态 / 摘要）
EVENT_JOB_FINISHED = "test/job-finished"
#: 用例失败批（聚合后，载荷含失败明细引用）
EVENT_CASE_FINISHED = "test/case-finished"
#: 任务终结（载荷含状态与计数摘要）
EVENT_TASK_FINISHED = "test/task-finished"
#: 产物登记完成（载荷含 uri / size / sha256）
EVENT_ARTIFACT_READY = "test/artifact-ready"
#: 任务详情接口的 extensions 收集槽（同 heartbeat/collect-node 模式）
EVENT_COLLECT_TASK = "test/collect-task"

# -- Hook 链名（§11.2，宿主 = test_core） ------------------------------------

#: submit 落库后、派发前改 spec（Stage 参数 / labels）
CHAIN_TASK_TRANSFORM = "task.transform"
#: test.job.result 落库前归一化（summary / cases）
CHAIN_RESULT_TRANSFORM = "result.transform"
#: test/artifact-ready 发出后旁路写（只读 ref）
CHAIN_ARTIFACT_SINK = "artifact.sink"

# -- 状态字面量（§5 状态机） -------------------------------------------------

type TaskState = Literal[
    "pending", "planning", "running", "canceling",
    "succeeded", "failed", "canceled", "error",
]
type JobState = Literal[
    "queued", "leased", "running",
    "succeeded", "failed", "canceled", "lost",
]
# 上行 JobResult.status 允许 "timeout"（执行器自报超时），落库归 job "failed"
type StageKind = Literal["run", "approval"]  # 执行阶段 / 人工确认门（预留）

# -- 计划标签键（§4.3：计划下发自动注入任务 labels） --------------------------

#: 计划 id 标签（任务回溯所属计划）
LABEL_PLAN_ID = "plan_id"
#: 计划名标签（已删计划仍可显示名，评审 #21）
LABEL_PLAN_NAME = "plan_name"


# -- 提交面（API/服务入参） --------------------------------------------------


@dataclass(frozen=True)
class ExecutorSelector:
    """执行器选择器：按能力信封匹配（common/capabilities.py 的 kind/id/labels）。

    台架定向语义（测试计划编辑器 v2 设计 §4）：``nodes`` 非空 = 台架 allowlist，
    候选节点取交集（仍需该台架具备本执行器能力），空 = 不指定（自动匹配）；
    ``strategy``：``balanced`` = 最少在飞优先（默认），``random`` = 匹配集合内
    随机派发（避开上次执行节点仍优先）。
    """

    id: str  # 能力 id，如 "pytest"
    kind: str = "executor"  # 固定大类
    labels: Mapping[str, str] = field(default_factory=dict[str, str])  # 可选筛选轴，如 {"lang": "python"}
    nodes: Sequence[str] = ()  # 指定台架 allowlist（空 = 不指定，设计 §4）
    strategy: Literal["balanced", "random"] = "balanced"  # 派发策略（设计 §4）


@dataclass(frozen=True)
class ShardSpec:
    """分片声明。"""

    mode: Literal["none", "plan"] = "none"  # 不分片 / 执行器规划分片
    hint: int = 0  # 期望分片数提示（0 = 执行器自定）


@dataclass(frozen=True)
class StageSpec:
    """任务的一个阶段（v1 线性顺序执行 + 屏障）。"""

    name: str
    executor: ExecutorSelector
    params: Mapping[str, Any]  # 执行器自有参数（核心不解析）
    shard: ShardSpec = ShardSpec()
    requires: Mapping[str, int] = field(default_factory=dict[str, int])  # 资源需求，如 {"canoe": 1}
    timeout_s: float = 0.0  # 0 = 取配置默认


@dataclass(frozen=True)
class TaskSpec:
    """任务提交规格（POST /api/test/tasks 的 body）。"""

    name: str
    stages: Sequence[StageSpec]  # v1 线性顺序执行 + 屏障
    priority: int = 0  # 大者先出队
    labels: Mapping[str, str] = field(default_factory=dict[str, str])  # 项目/租户轴，配额与筛选
    params: Mapping[str, Any] = field(default_factory=dict[str, Any])  # 任务级参数（注入各 Stage）
    idempotency_key: str = ""  # 提交去重（CI 重复触发）
    failure_policy: Literal["fail_fast", "continue"] = "fail_fast"
    timeout_s: float = 0.0  # 任务总超时，0 = 不限


# -- 下行面（master → slave，topic: test.job） -------------------------------


@dataclass(frozen=True)
class JobDescriptor:
    """派发子运行的作业描述（§7 载荷，幂等键 (job_id, attempt)）。"""

    proto: int  # 协议版本，执行器不认识即拒收（§8.3）
    job_id: str
    task_id: str
    stage_name: str
    attempt: int  # 从 1 起
    executor: ExecutorSelector
    params: Mapping[str, Any]
    shard: Mapping[str, Any] | None  # {index, total, unit_refs}，None = 不分片
    artifact_dir: str  # 产物目录（节点本地路径约定见 §8.4）
    deadline_ts: float  # 建议完成时刻（节点本地参考）


# -- 上行面（slave → master，topic: test.job.result） ------------------------


@dataclass(frozen=True)
class LogChunk:
    """测试输出流小包（§11.2 / §10.4）；lines 每项 {ts: float, level: int, msg: str}。"""
    proto: int
    job_id: str
    attempt: int
    seq: int
    stream: Literal["stdout", "stderr", "case"]
    case_id: str = ""
    lines: Sequence[Mapping[str, Any]] = field(default_factory=list[Mapping[str, Any]])


@dataclass(frozen=True)
class ArtifactRef:
    """产物引用。"""

    artifact_id: str
    kind: Literal["log", "report", "attachment"]
    name: str
    uri: str  # local://… → oss://…（消费插件回填）
    size: int
    sha256: str


@dataclass(frozen=True)
class CaseResult:
    """单条用例结果。"""

    case_id: str  # 用例稳定标识（执行器定义）
    status: Literal["passed", "failed", "skipped", "error", "xfailed", "xpassed"]
    duration_s: float
    message: str = ""  # 失败摘要（截断，详情进产物）
    detail_ref: str = ""  # 产物/详情引用 id


@dataclass(frozen=True)
class JobResult:
    """作业结果上报（§7 载荷，幂等键 (job_id, attempt)）。"""

    proto: int
    job_id: str
    attempt: int
    status: Literal["succeeded", "failed", "canceled", "timeout"]
    summary: Mapping[str, Any]  # {total, passed, failed, ...} 执行器自定
    cases: Sequence[CaseResult]  # 上限后截断，全量进报告产物
    artifacts: Sequence[ArtifactRef]
    started_ts: float
    finished_ts: float
    error: str = ""  # 基础设施级错误（区别于用例失败）


# -- 异常族（住 common/，消费插件经本模块捕获，不 import 插件包） ------------


class TestCoreError(Exception):
    """test_core 错误族基类。"""


class TaskNotFound(TestCoreError):
    """任务不存在。"""


class InvalidSpec(TestCoreError):
    """TaskSpec 不合法（提交被拒）。"""


class InvalidTransition(TestCoreError):
    """非法状态迁移（终态不可逆等，见 §5 推进规则）。"""


class DuplicateSubmission(TestCoreError):
    """重复提交（idempotency_key 冲突）。"""


class MaterialNotFound(TestCoreError):
    """材料不存在。"""


class MaterialInUse(TestCoreError):
    """材料被引用（任务 params / 计划步骤 / 选择集在用），禁止删除。"""


class PlanNotFound(TestCoreError):
    """测试计划不存在。"""


# -- 服务门面（ctx.test_core，§6） -------------------------------------------


@runtime_checkable
class TestCoreInterface(Protocol):
    """test_core 核心服务门面（``ctx.test_core``）。

    全部触库 / 穿 Hook 链的方法声明为 ``async``；消费插件的只读需求也走这里
    （不许直连 db 表）。实现方为 ``TestCorePlugin``。
    """

    # -- 提交与生命周期（写） -------------------------------------------------

    async def submit(self, spec: TaskSpec, *, source: str = "api") -> str:
        """提交任务，返回 task_id。idempotency_key 命中则返回既有任务。"""
        ...

    async def cancel(self, task_id: str, reason: str = "") -> bool:
        """请求取消任务；已终结或不存在返回 False。"""
        ...

    async def rerun_failed(self, task_id: str) -> str:
        """重建失败 Job（同 Task 内 attempt+1），返回同一 task_id。"""
        ...

    async def set_priority(self, task_id: str, priority: int) -> bool:
        """调整任务优先级；任务不存在或已终结返回 False。"""
        ...

    # -- 查询（只读，消费插件入口） -------------------------------------------

    async def get_task(self, task_id: str) -> Mapping[str, Any] | None:
        """任务详情（含 extensions 收集槽）；不存在返回 None。"""
        ...

    async def list_tasks(
        self,
        *,
        state: str | None = None,
        labels: Mapping[str, str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        """任务列表（state / labels 过滤、分页）。"""
        ...

    async def list_jobs(self, task_id: str) -> list[Mapping[str, Any]]:
        """任务的 Job 列表（含 attempt 历史）。"""
        ...

    async def list_task_history(
        self, task_id: str, *, limit: int = 200
    ) -> list[Mapping[str, Any]]:
        """任务状态迁移时间线（管理页时间线 / 消费插件审计用）。"""
        ...

    async def get_result(self, task_id: str) -> Mapping[str, Any] | None:
        """聚合结果 + 各 Job/attempt 明细引用；任务不存在返回 None。"""
        ...

    async def list_case_results(
        self,
        *,
        task_id: str = "",
        job_id: str = "",
        status: str = "",
        limit: int = 200,
    ) -> list[Mapping[str, Any]]:
        """用例结果查询（task_id / job_id / status 过滤）。"""
        ...

    async def list_artifacts(
        self, *, task_id: str = "", state: str = "local"
    ) -> list[Mapping[str, Any]]:
        """产物列表（state 过滤，默认本地产物）。"""
        ...

    async def mark_artifact(
        self, artifact_id: str, *, uri: str | None = None, state: str | None = None
    ) -> bool:
        """产物状态/URI 回填（artifact_oss / artifact_gc 专用写口，仅此一个）。"""
        ...

    # -- 材料服务面（材料 / 用例清单 / 选择集，D10） ---------------------------

    async def upload_material(
        self,
        *,
        name: str,
        data: bytes,
        recipe: str = "",
        source: str = "api",
        created_by: str = "",
        labels: Mapping[str, str] | None = None,
        executor_id: str = "",
    ) -> Mapping[str, Any]:
        """落盘+入库；(sha256, recipe_fingerprint) 命中返回既有材料 dict（幂等）；返回含 id/state/…。

        ``recipe`` 为执行器自定义不透明字符串（core 不解析、不校验，原样存取）；
        ``executor_id`` 为材料归属执行器（空 = 通用材料，§4.5）。
        """
        ...

    async def list_materials(
        self,
        *,
        state: str = "",
        labels: Mapping[str, str] | None = None,
        limit: int = 50,
        offset: int = 0,
        executor_id: str = "",
    ) -> list[Mapping[str, Any]]:
        """材料列表（state / labels 过滤、分页）。

        ``executor_id`` 过滤谓词 ``executor_id == X OR executor_id == ""``
        （通用材料恒可见）；空串 = 不按归属过滤（§4.5）。
        """
        ...

    async def get_material(self, material_id: str) -> Mapping[str, Any] | None:
        """材料详情（返回 dict 中 ``recipe`` 为执行器自定义不透明字符串）；不存在返回 None。"""
        ...

    async def get_material_bytes(self, material_id: str) -> bytes | None:
        """材料 zip 本体；不存在返回 None。"""
        ...

    async def update_recipe(
        self, material_id: str, recipe: str
    ) -> Mapping[str, Any]:
        """更新配方（执行器自定义不透明字符串，core 不解析、原样存取），状态回
        uploaded（需重分析）；不存在抛 MaterialNotFound。"""
        ...

    async def put_problems(
        self, material_id: str, problems: Sequence[Mapping[str, Any]], *, state: str
    ) -> bool:
        """inspect 结果写入（全量替换）；state ∈ {"analyzed","failed"}；不存在返回 False。"""
        ...

    async def put_cases(
        self, material_id: str, cases: Sequence[Mapping[str, Any]]
    ) -> int:
        """全量替换该材料用例清单，返回条数。"""
        ...

    async def list_material_cases(self, material_id: str) -> list[Mapping[str, Any]]:
        """材料的用例清单（material_case 行）。"""
        ...

    async def save_selection(
        self, material_id: str, name: str, case_ids: Sequence[str], *, created_by: str = ""
    ) -> str:
        """保存选择集，返回 selection_id。"""
        ...

    async def get_selection(self, selection_id: str) -> Mapping[str, Any] | None:
        """选择集详情；不存在返回 None。"""
        ...

    async def delete_material(self, material_id: str) -> bool:
        """删除材料；有引用（任务 params 引用 / 选择集在用）抛 MaterialInUse，不存在抛 MaterialNotFound。"""
        ...

    async def list_unreferenced(self, *, older_than_ts: float) -> list[Mapping[str, Any]]:
        """GC 辅助：无引用且 updated_ts < older_than_ts 的材料列表。"""
        ...

    # -- 计划服务面（测试计划 CRUD / 下发，§4.4） ------------------------------

    async def save_plan(
        self,
        *,
        plan_id: str = "",
        name: str,
        description: str = "",
        labels: Mapping[str, str] | None = None,
        defaults: Mapping[str, Any] | None = None,
        steps: Sequence[Mapping[str, Any]] | None = None,
        trigger: Mapping[str, Any] | None = None,
        notify: Mapping[str, Any] | None = None,
        created_by: str = "",
    ) -> Mapping[str, Any]:
        """新建或更新计划；plan_id 空 = 新建。更新不存在抛 PlanNotFound，校验失败抛 InvalidSpec。

        ``trigger`` / ``notify`` 为预留接口（设计 §5）：``{type, config}`` 对象，
        缺省 ``manual`` / ``none``；枚举校验在核心（trigger.type ∈ manual/cron/event，
        notify.type ∈ none/jira/webhook/email），其余类型照存照显。
        """
        ...

    async def list_plans(
        self,
        *,
        keyword: str = "",
        labels: Mapping[str, str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        """计划列表（keyword 模糊匹配 name/description、labels 子集匹配、分页）。"""
        ...

    async def get_plan(self, plan_id: str) -> Mapping[str, Any] | None:
        """计划详情；不存在返回 None。"""
        ...

    async def delete_plan(self, plan_id: str) -> bool:
        """删除计划；不存在返回 False。"""
        ...

    async def dispatch_plan(
        self, plan_id: str, overrides: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        """下发计划实例化任务，返回 {task_id, deduplicated}。

        缺失抛 PlanNotFound；overrides 未知键 / 校验失败抛 InvalidSpec；
        source 固定 "plan-dispatch"（幂等域隔离，评审 #13）。
        """
        ...

    # -- 扩展点（HookChain 宿主） ---------------------------------------------

    def register_hook(
        self,
        chain: str,
        fn: Callable[..., Any],
        *,
        phase: str,
        order: int,
        name: str,
        on_error: str = "fail",
    ) -> Callable[[], None]:
        """登记拦截器，返回注销函数（绑调用方 fiber）。

        ``chain`` 取 :data:`CHAIN_TASK_TRANSFORM` / :data:`CHAIN_RESULT_TRANSFORM`
        / :data:`CHAIN_ARTIFACT_SINK`；``on_error`` 取 ``"fail"``（出错上抛，
        fail-closed，鉴权类）或 ``"skip"``（记问题后放行，fail-open，统计 /
        日志类）。语义见 DESIGN §11.2。
        """
        ...


__all__: list[str] = [
    "CHAIN_ARTIFACT_SINK",
    "CHAIN_RESULT_TRANSFORM",
    # Hook 链名
    "CHAIN_TASK_TRANSFORM",
    "EVENT_ARTIFACT_READY",
    "EVENT_CASE_FINISHED",
    "EVENT_COLLECT_TASK",
    "EVENT_JOB_FINISHED",
    # 事件名
    "EVENT_TASK_CREATED",
    "EVENT_TASK_FINISHED",
    "EVENT_TASK_STATE",
    # 计划标签键
    "LABEL_PLAN_ID",
    "LABEL_PLAN_NAME",
    "SCHEMA_VERSION",
    # 版本与协议常量
    "TEST_PROTO",
    # 通信 topic
    "TOPIC_JOB",
    "TOPIC_JOB_CANCEL",
    "TOPIC_JOB_LOG",
    "TOPIC_JOB_PROGRESS",
    "TOPIC_JOB_RESULT",
    # 上行面
    "ArtifactRef",
    "CaseResult",
    "DuplicateSubmission",
    # 提交面
    "ExecutorSelector",
    "InvalidSpec",
    "InvalidTransition",
    # 下行面
    "JobDescriptor",
    "JobResult",
    "JobState",
    "LogChunk",
    "MaterialInUse",
    "MaterialNotFound",
    "PlanNotFound",
    "ShardSpec",
    "StageKind",
    "StageSpec",
    "TaskNotFound",
    "TaskSpec",
    # 状态字面量
    "TaskState",
    # 异常族
    "TestCoreError",
    # 服务门面
    "TestCoreInterface",
]
