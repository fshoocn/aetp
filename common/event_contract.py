"""事件派发模式契约（GAP-2 的机器化根治；设计文档附录 G #3 / Phase 0.6）。

cordis 事件的**派发模式是公开契约**（DeepSeek Harness ``@mode`` 同款）：
``emit`` **不执行** ``async def`` 监听器（协程被静默丢弃，实测 E2），因此每个
事件在本注册表声明它允许的派发方式，``tests/test_event_contract.py`` 扫描
全仓库派发点与声明的一致性——「事件一律 parallel」从评审口令升级为机器检查。

约定：

* ``emit`` 事件的监听器必须是**同步**函数（或自行派生任务）；
* 可能有 ``async def`` 监听器的事件用 ``parallel``（推荐经
  :func:`common.cordis_utils.parallel_event` 派发，异常被隔离并返回）；
* 动态派发点（事件名是变量，如 comm 的 topic 派发）不在扫描范围，
  新增时**人工**对照本表声明。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: 派发模式（cordis 五种派发）
type EventMode = Literal["emit", "waterfall", "parallel", "serial", "bail"]


@dataclass(frozen=True, slots=True)
class EventSpec:
    """一条事件的公开契约。"""

    name: str
    mode: EventMode
    summary: str


def declare_event(name: str, mode: EventMode, summary: str) -> EventSpec:
    """登记一条事件契约；同名重复声明必须完全一致（不静默覆盖）。"""
    spec = EventSpec(name, mode, summary)
    existing = EVENTS.get(name)
    if existing is not None and existing != spec:
        raise ValueError(f"事件 {name!r} 重复声明且不一致：{existing} vs {spec}")
    EVENTS[name] = spec
    return spec


def event_mode(name: str) -> EventMode | None:
    """查询事件声明的派发模式；未声明返回 ``None``。"""
    spec = EVENTS.get(name)
    return spec.mode if spec is not None else None


#: 事件注册表（key = 事件名）
EVENTS: dict[str, EventSpec] = {}

for _spec in (
    EventSpec(
        "db/schema-changed",
        "emit",
        "db 结构变更通知（同步监听器：派生任务自理，见 db README 的会话隔离约定）",
    ),
    EventSpec(
        "heartbeat/node-status",
        "emit",
        "从节点状态转换广播（⚠️ 仅同步监听器；改 parallel 见 Q7）",
    ),
    EventSpec(
        "heartbeat/collect-nodes",
        "parallel",
        "节点列表接口的扩展收集槽（监听器把自家数据写进 response）",
    ),
    EventSpec(
        "heartbeat/collect-node",
        "parallel",
        "节点详情接口的扩展收集槽",
    ),
    EventSpec(
        "heartbeat/collect-status",
        "parallel",
        "从节点 status 接口的扩展收集槽",
    ),
    EventSpec(
        "heartbeat/collect-capabilities",
        "parallel",
        "能力条目收集槽（从节点心跳侧，监听器把信封 append 进桶）",
    ),
    EventSpec(
        "webapi/auth-denied",
        "parallel",
        "鉴权拒绝广播（载荷 {path, method, reason, remote}，审计 / 统计订阅）",
    ),
):
    declare_event(_spec.name, _spec.mode, _spec.summary)

__all__: list[str] = ["EVENTS", "EventMode", "EventSpec", "declare_event", "event_mode"]
