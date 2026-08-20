"""Hook 端口（P3.7，§10.4/§10.6）。

两类 Hook 不可混用：
- AdmissionHook（准入/策略 Hook）：数据库事务提交前评估，可阻止业务操作，
  默认 fail closed；输入不可变，不得写库/发 MQTT/调外部 HTTP。
- EventHook（提交后事件 Hook）：领域事件持久化后异步消费，默认 fail open，
  失败不影响已提交业务状态。

Hook 由 bootstrap 容器显式注册；stage 内按 (order, name) 稳定排序（§10.6）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from master.domain.models import DomainEvent


@dataclass(frozen=True)
class HookContext:
    """准入 Hook 评估上下文（不可变输入）。"""

    # sym:stage 触发阶段（如 run.before_dispatch，见 §10.4 推荐清单）
    stage: str
    # sym:project_id 所属项目业务标识（平台级为空）
    project_id: str | None = None
    # sym:payload 阶段相关结构化输入（命令/请求载荷）
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HookDecision:
    """准入 Hook 决策。"""

    # sym:allowed 是否放行
    allowed: bool
    # sym:reason 决策说明（deny 时给用户可读原因）
    reason: str = ""
    # sym:code 机器可读错误码（deny 时返回，如 NODE_CAPABILITY_MISMATCH）
    code: str | None = None
    # sym:advisory 仅提醒不阻止（fail open 的显式标记）
    advisory: bool = False


class AdmissionHook(Protocol):
    """准入 Hook：事务提交前评估，可阻止业务操作（§10.6 fail closed）。"""

    # sym:name Hook 全局唯一名
    name: str
    # sym:stage 绑定阶段
    stage: str
    # sym:order stage 内执行顺序（同 stage 按 (order, name) 稳定排序）
    order: int

    async def evaluate(self, context: HookContext) -> HookDecision:
        """评估命令/操作是否放行。"""
        ...


class EventHook(Protocol):
    """提交后事件 Hook：领域事件持久化后异步消费（§10.6 fail open）。"""

    # sym:name Hook 全局唯一名
    name: str
    # sym:event_types 关注的事件类型集合（空集合订阅全部）
    event_types: frozenset[str]

    async def handle(self, event: DomainEvent) -> None:
        """处理一个已持久化的领域事件。"""
        ...
