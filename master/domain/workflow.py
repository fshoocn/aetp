"""执行编排层：工作流即数据（WorkflowSpec）。

把聚合生命周期（脚本：uploaded→verify→parse→ready；Run：created→split→
dispatch→...）建模为**显式 WorkflowSpec**（阶段图），由 WorkflowEngine
统一推进，取代"散落在服务方法里的隐式时序"。本模块只含纯数据与纯函数
（next_stage），不依赖 adapter。

与现有状态机（state_machine.py）的关系：
- state_machine 校验"状态迁移合法性"（聚合 status 的微观合法性）
- WorkflowSpec 描述"业务阶段编排"（阶段序列、动作、超时、重试、去向）
二者互补：阶段推进时仍用状态机校验聚合 status 迁移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class WorkflowStage:
    """工作流阶段定义（WorkflowSpec 的节点）。"""

    # sym:name 阶段名（唯一，作为 stage 键）
    name: str
    # sym:action 动作名（引擎经 WorkflowActionRunner 路由到具体实现：调插件/发 MQTT/落库）
    action: str = ""
    # sym:timeout_s 动作超时（秒，可亚秒）；0 = 无超时
    timeout_s: float = 0
    # sym:retry 动作失败重试次数（0 = 不重试）
    retry: int = 0
    # sym:on_success 成功后下一阶段；空 = 进入成功终态
    on_success: str = ""
    # sym:on_failure 失败后去向；空 = 进入失败终态
    on_failure: str = ""


@dataclass(frozen=True)
class WorkflowSpec:
    """聚合生命周期工作流定义（工作流即数据）。"""

    # sym:aggregate_type 聚合类型（script / task_run / ...）
    aggregate_type: str
    # sym:start 起始阶段名
    start: str
    # sym:stages 阶段图（name -> WorkflowStage）
    stages: Mapping[str, WorkflowStage]
    # sym:terminal_success 成功终态名
    terminal_success: str
    # sym:terminal_failure 失败终态名
    terminal_failure: str

    def __post_init__(self) -> None:
        """不变量校验：起始/终态必须在阶段图中；去向必须指向存在的阶段。"""
        if self.start not in self.stages:
            raise ValueError(f"起始阶段不存在: {self.start}")
        for name, terminal in (
            (self.terminal_success, "成功"),
            (self.terminal_failure, "失败"),
        ):
            if name not in self.stages:
                raise ValueError(f"{terminal}终态不在阶段图: {name}")
        for name, stage in self.stages.items():
            for target in (stage.on_success, stage.on_failure):
                if target and target not in self.stages:
                    raise ValueError(
                        f"阶段 {name} 的去向不存在: {target}"
                    )

    def next_stage(self, current: str, ok: bool) -> str:
        """纯函数：当前阶段执行结果 → 下一阶段（终态由 on_success/on_failure 或默认终态决定）。"""
        stage = self.stages[current]
        if ok:
            return stage.on_success or self.terminal_success
        return stage.on_failure or self.terminal_failure


@dataclass
class WorkflowProgress:
    """工作流进度（内存推进；持久化由调用方负责）。"""

    # sym:aggregate_id 聚合业务标识（script_id / run_id）
    aggregate_id: str
    # sym:stage 当前阶段名
    stage: str
    # sym:attempts 当前阶段已尝试次数（含重试）
    attempts: int = 0
    # sym:error 最近失败信息（阶段失败/超时摘要）
    error: str | None = None
    # sym:context 阶段动作上下文（script_dir/config/case_filter 等，动作输入）
    context: dict = field(default_factory=dict)

    def is_terminal(self, spec: WorkflowSpec) -> bool:
        """当前是否处于工作流终态（成功/失败）。"""
        return self.stage in (spec.terminal_success, spec.terminal_failure)


# ---------------------------------------------------------------------------
# 参考工作流：测试脚本（uploaded → verify → parse → ready / failed）
# 与 §18.3 流程对应；P4 接入 MQTT 后按 verify_location 分派 verify/parse。
# ---------------------------------------------------------------------------

SCRIPT_WORKFLOW = WorkflowSpec(
    aggregate_type="script",
    start="uploaded",
    stages={
        "uploaded": WorkflowStage(
            name="uploaded",
            action="persist_script",
            on_success="verify",
        ),
        "verify": WorkflowStage(
            name="verify",
            action="verify_script",
            timeout_s=30,
            retry=2,
            on_success="parse",
            on_failure="failed",
        ),
        "parse": WorkflowStage(
            name="parse",
            action="parse_cases",
            timeout_s=120,
            retry=1,
            on_success="ready",
            on_failure="failed",
        ),
        "ready": WorkflowStage(name="ready"),
        "failed": WorkflowStage(name="failed"),
    },
    terminal_success="ready",
    terminal_failure="failed",
)
