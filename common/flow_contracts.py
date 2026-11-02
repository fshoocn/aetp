"""流程编排契约（非插件公共层）：流程定义 / 步骤接口 / 运行状态 / 事件名。

插件铁律：步骤提供方、流程发起方与流程引擎三者之间只经 ``ctx.flow`` 与本层类型
交互，不得互相 import 插件包。本层只放**纯数据与协议**，不含运行时实现。

设计要点（详见 ``docs/链式执行与流程编排设计-2026-10-26.md``）：

* 流程定义是**数据**，节点用「阶段 + 标签」选择器描述需要的能力，不出现插件名；
* 步骤提供方经 ``ctx.flow.step(...)`` 自注册，注册绑定调用方 fiber，卸载自动摘除；
* 步骤的发现沿用 ``common.capabilities`` 能力信封：``kind = "flow.step"``，
  阶段放 ``labels["stage"]``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol, TypedDict

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type StepOutput = Mapping[str, JsonValue] | None

#: 步骤在能力信封里的 kind；阶段放 labels[STAGE_LABEL]
STEP_KIND: Final = "flow.step"
STAGE_LABEL: Final = "stage"

#: 生命周期事件（一律经 ``parallel`` 派发，载荷是可 JSON 序列化的 dict）
EVT_RUN_STARTED: Final = "flow/run-started"
EVT_NODE_FINISHED: Final = "flow/node-finished"
EVT_RUN_FINISHED: Final = "flow/run-finished"
EVT_RUN_INTERRUPTED: Final = "flow/run-interrupted"
EVT_STEPS_CHANGED: Final = "flow/steps-changed"

#: 远程作业协议版本（附录 E.2 的 ``proto`` 字段）
REMOTE_PROTO: Final = 1

#: 远程作业消息 topic（附录 E.2；点分风格沿用 ``heartbeat.beat`` / ``log.batch``）
TOPIC_STEP_RUN: Final = "flow.step.run"
TOPIC_STEP_LEASE: Final = "flow.step.lease"
TOPIC_STEP_RESULT: Final = "flow.step.result"
TOPIC_STEP_CANCEL: Final = "flow.step.cancel"


class Mode(StrEnum):
    """一个节点匹配到多个提供者时的取舍。"""

    ONE = "one"
    ALL = "all"


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class NodeState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ProblemLevel(StrEnum):
    """``ERROR`` 拒绝启动 / 保存；``WARN`` 放行但提示。"""

    ERROR = "error"
    WARN = "warn"


class ProblemCode(StrEnum):
    NO_PROVIDER = "no_provider"
    AMBIGUOUS = "ambiguous"
    LOCKED = "locked"
    UNKNOWN_NODE = "unknown_node"
    UNKNOWN_SLOT = "unknown_slot"
    DUPLICATE_NODE = "duplicate_node"
    BAD_PARAMS = "bad_params"


@dataclass(frozen=True, slots=True)
class NodeDef:
    """流程里的一个节点：选择器 + 执行策略（不含任何插件名）。"""

    id: str
    stage: str
    labels: Mapping[str, str] = field(default_factory=dict[str, str])
    pin: str | None = None
    mode: Mode = Mode.ONE
    required: bool = True
    locked: bool = False
    always: bool = False
    params: Mapping[str, JsonValue] = field(default_factory=dict[str, JsonValue])
    retries: int = 0
    backoff: float = 1.0
    timeout: float | None = None


@dataclass(frozen=True, slots=True)
class SlotDef:
    """模板声明的插入位：允许在 ``after`` 节点之后插入哪些阶段的节点。"""

    id: str
    after: str
    stages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FlowDef:
    id: str
    version: int
    nodes: tuple[NodeDef, ...]
    slots: tuple[SlotDef, ...] = ()
    title: str = ""


class InsertSpec(TypedDict):
    after: str
    node: dict[str, JsonValue]


class Overrides(TypedDict, total=False):
    """用户 / 单次运行对模板的覆盖：只改「允许改」的部分（锁定节点不可动）。"""

    disable: list[str]
    insert: list[InsertSpec]
    params: dict[str, dict[str, JsonValue]]
    pin: dict[str, str]
    labels: dict[str, dict[str, str]]


@dataclass(frozen=True, slots=True)
class Problem:
    """预检 / 校验发现的问题。"""

    level: ProblemLevel
    node: str
    code: ProblemCode
    message: str


@dataclass(frozen=True, slots=True)
class StepContext:
    """交给步骤处理函数的现场（可 JSON 序列化的部分便于远程执行）。

    ``data`` 是上游节点输出的只读快照，按节点 id 分区；``cancelled`` 置位表示
    运行被取消 / 步骤超时，应尽快收尾（引擎在 ``cancel_grace`` 后强制取消）。
    """

    run_id: str
    flow_id: str
    node_id: str
    attempt: int
    params: Mapping[str, JsonValue]
    input: Mapping[str, JsonValue]
    data: Mapping[str, JsonValue]
    cancelled: asyncio.Event


class RemoteJobState(StrEnum):
    """一个远程作业的结局（:class:`RemoteResult.state`）。"""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    #: 租约超时且不可安全重试（§5.11.5）：结果未知，run 需打断处置
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RemoteResult:
    """一次远程作业的结果（§5.11.3 ``run_remote`` 的返回项）。"""

    job_id: str
    state: RemoteJobState
    output: JsonValue = None
    error: str = ""
    #: 实际执行节点（换节点重试后是最后一跳）
    node: str = ""
    #: 总尝试次数（含重试）
    attempts: int = 0
    duration_ms: float = 0.0


class StepSkipped(Exception):
    """步骤主动声明「本次无需执行」（记 skipped，不算失败）。"""


class StepRetryable(Exception):
    """可重试的失败（对端暂不可用等）；不可重试的失败直接抛普通异常。"""


class FlowError(RuntimeError):
    """流程引擎的业务错误基类（§7.2）。"""


class FlowRejected(FlowError):
    """预检未通过而拒绝启动：携带全部 :class:`Problem`（§5.9）。"""

    def __init__(self, problems: list[Problem]) -> None:
        super().__init__(
            f"预检未通过：{sum(1 for p in problems if p.level is ProblemLevel.ERROR)} 个错误"
        )
        self.problems: list[Problem] = problems


#: 处理函数同步 / 异步均可，返回 JSON 可序列化的字典（写入黑板）或 ``None``
type StepHandler = Callable[[StepContext], StepOutput | Awaitable[StepOutput]]


class StepRegistration(Protocol):
    """``ctx.flow.step`` 的返回值：注册句柄。"""

    @property
    def alive(self) -> bool: ...

    async def drain(self, timeout: float | None = None) -> None:
        """停接新任务并等在途尝试结束（幂等；提供方自己的有序卸载里先调它再释放资源）。"""
        ...

    def close(self) -> None: ...


class RunHandle(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def state(self) -> RunState: ...

    async def wait(self, timeout: float | None = None) -> RunState: ...


class FlowInterface(Protocol):
    """``ctx.flow`` 的类型面。"""

    def step(
        self,
        stage: str,
        step_id: str,
        handler: StepHandler,
        *,
        labels: Mapping[str, str] | None = None,
        priority: int = 0,
        version: str = "",
        summary: str = "",
        params_schema: Mapping[str, JsonValue] | None = None,
        max_concurrency: int = 0,
        idempotent: bool = False,
    ) -> StepRegistration: ...

    def define(self, flow: FlowDef) -> Callable[[], None]: ...

    def catalog(
        self,
        *,
        stage: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> list[dict[str, object]]: ...

    def preflight(self, flow_id: str, overrides: Overrides | None = None) -> list[Problem]: ...

    async def start(
        self,
        flow_id: str,
        input: Mapping[str, JsonValue] | None = None,
        *,
        overrides: Overrides | None = None,
        idempotency_key: str | None = None,
        actor: str | None = None,
    ) -> RunHandle: ...

    async def cancel(self, run_id: str, reason: str = "", *, actor: str | None = None) -> bool: ...

    async def run_remote(
        self,
        sc: StepContext,
        *,
        stage: str,
        where: Mapping[str, str] | None = None,
        jobs: Sequence[Mapping[str, JsonValue]],
        max_parallel: int = 8,
        retries: int = 0,
        timeout: float = 1800.0,
    ) -> list[RemoteResult]:
        """远程作业原语（§5.11.3）：引擎管机制（选节点 / 租约 / 重试 / 去重），
        提供者管策略（分片 / 参数 / 结果后处理）。"""
        ...


__all__: list[str] = [
    "EVT_NODE_FINISHED",
    "EVT_RUN_FINISHED",
    "EVT_RUN_INTERRUPTED",
    "EVT_RUN_STARTED",
    "EVT_STEPS_CHANGED",
    "REMOTE_PROTO",
    "STAGE_LABEL",
    "STEP_KIND",
    "TOPIC_STEP_CANCEL",
    "TOPIC_STEP_LEASE",
    "TOPIC_STEP_RESULT",
    "TOPIC_STEP_RUN",
    "FlowDef",
    "FlowError",
    "FlowInterface",
    "FlowRejected",
    "InsertSpec",
    "JsonValue",
    "Mode",
    "NodeDef",
    "NodeState",
    "Overrides",
    "Problem",
    "ProblemCode",
    "ProblemLevel",
    "RemoteJobState",
    "RemoteResult",
    "RunHandle",
    "RunState",
    "SlotDef",
    "StepContext",
    "StepHandler",
    "StepOutput",
    "StepRegistration",
    "StepRetryable",
    "StepSkipped",
]
