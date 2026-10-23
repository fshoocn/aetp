"""comm 契约层：投递策略 / 消息信封 / 业务接口 / 传输驱动接口。

业务插件只依赖本模块类型 + ``ctx.comm``（实现 :class:`CommInterface`）；
传输实现依赖 :class:`CommTransport`——协议单选（决策 A2）：HTTP 是默认实现，
将来换协议（如 MQTT）由新实现**替换**默认装配，QoS 语义映射见
``docs/comm通信方案-2026-10-22.md`` §4.2（fire-and-forget → QoS0 /
at-least-once → QoS1，不用 QoS2）。
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol, TypedDict, runtime_checkable

from common.contracts import CommMeta


class DeliveryPolicy(TypedDict, total=False):
    """单条消息的投递语义（三参数正交，未填取默认值）。

    * ``min_success``：至少发送成功次数——**任意一次成功即可**（决策 A4），
      广播即"任一目标成功"；默认 1；
    * ``min_retries``：至少重试次数——放弃前的保底重试；默认 0；
    * ``max_attempts``：至多发送次数（含首次，硬上限）；默认 0 = 不限。
    """

    min_success: int
    min_retries: int
    max_attempts: int


#: 发完即弃：不落盘、不重试（心跳等"容忍丢失"流量）
FIRE_AND_FORGET: DeliveryPolicy = {"min_success": 0, "max_attempts": 1}
#: at-least-once：成功前无限重试（默认档，消息持久化、重启续传）
AT_LEAST_ONCE: DeliveryPolicy = {"min_success": 1, "min_retries": 0, "max_attempts": 0}


def bounded_policy(min_retries: int, max_attempts: int) -> DeliveryPolicy:
    """至少重试 ``min_retries`` 次、至多发送 ``max_attempts`` 次（含首次）。

    两者冲突（``min_retries + 1 > max_attempts``）时**硬上限优先**（决策 D2），
    即最多仍只发 ``max_attempts`` 次。
    """
    return {
        "min_success": 1,
        "min_retries": max(0, int(min_retries)),
        "max_attempts": max(1, int(max_attempts)),
    }


def resolve_policy(
    policy: DeliveryPolicy | None, *, default: DeliveryPolicy
) -> DeliveryPolicy:
    """按默认值合并出完整策略并钳位到合法区间。"""
    source: DeliveryPolicy = policy if policy is not None else default
    return {
        "min_success": max(0, source.get("min_success", default.get("min_success", 1))),
        "min_retries": max(0, source.get("min_retries", default.get("min_retries", 0))),
        "max_attempts": max(0, source.get("max_attempts", default.get("max_attempts", 0))),
    }


class MessageState(StrEnum):
    """消息（投递记录）生命周期状态。"""

    PENDING = "pending"
    RETRY_WAIT = "retry_wait"
    IN_FLIGHT = "in_flight"
    DONE = "done"
    FAILED = "failed"


#: topic 处理器：同步或异步均可
CommTopicHandler = Callable[[Any, CommMeta], Any]


class MessageStatus(TypedDict):
    """``status()`` 返回的单条消息投递状态。"""

    id: str
    kind: str
    dest: str
    topic: str
    state: str
    attempts: int
    acks: int
    last_error: str
    created_ts: float
    updated_ts: float


#: 线上信封（``from`` 是关键字，TypedDict 用函数式语法声明）
Envelope = TypedDict(
    "Envelope",
    {
        "id": str,
        "kind": str,
        "from": str,
        "to": str,
        "topic": str,
        "payload": Any,
        "ts": float,
        "ttl": float,
    },
)


class TransportResult(TypedDict):
    """传输层单次投递结果；``error_kind`` 决定 Core 是否重试。

    * ``""``：成功；
    * ``"network"``：网络失败 / 5xx / 429——可重试；
    * ``"protocol"``：4xx 协议错误（对端明确拒绝）——不可重试（决策 D3）。
    """

    ok: bool
    error_kind: str


class CommTransport(Protocol):
    """传输驱动契约：只回答「送到没有 / 有没有下行件 / 确认这些 id」。

    重试、退避、QoS 状态机全部在 Core，与驱动无关。
    """

    async def deliver(self, items: list[Envelope]) -> TransportResult:
        """把一批信封送到对端；只报告结果，不做重试。"""
        ...

    async def pull(self, hang: float) -> list[Envelope]:
        """拉取下行信封（长轮询挂起 ``hang`` 秒 / 阻塞订阅）。"""
        ...

    async def ack(self, ids: list[str]) -> None:
        """确认下行已处理，对端清理 outbox。"""
        ...

    async def close(self) -> None:
        """释放传输资源（连接等）。"""
        ...


@runtime_checkable
class CommInterface(Protocol):
    """主从通信服务的对外契约（``ctx.comm`` 的类型面）。

    三个发送方法正交完备，均**入队即返回** ``message_id``：

    * :meth:`broadcast`：一对全（汇聚点扇出；任一目标成功即达标）；
    * :meth:`send_to`：一对一（定向）；
    * :meth:`report`：一对汇聚点（"从节点 → 主节点"的高频入口）。
    """

    def broadcast(
        self,
        topic: str,
        payload: Any,
        *,
        policy: DeliveryPolicy | None = None,
        ttl: float | None = None,
        immediate: bool = False,
    ) -> str:
        """广播到全部节点（含本节点本地派发）。返回 message_id。"""
        ...

    def send_to(
        self,
        node: str,
        topic: str,
        payload: Any,
        *,
        policy: DeliveryPolicy | None = None,
        ttl: float | None = None,
        immediate: bool = False,
    ) -> str:
        """定向到指定节点。返回 message_id。"""
        ...

    def report(
        self,
        topic: str,
        payload: Any,
        *,
        policy: DeliveryPolicy | None = None,
        ttl: float | None = None,
        immediate: bool = False,
    ) -> str:
        """上行到汇聚点（主节点）；本节点即汇聚点时本地自环。返回 message_id。"""
        ...

    def on(self, topic: str, handler: CommTopicHandler) -> Callable[[], None]:
        """注册 topic 处理器；绑调用方 fiber，卸载自动注销。

        与 ``ctx.on(f"comm/{topic}", handler)`` 等价（cordis 事件总线派发）。
        """
        ...

    def nodes(self) -> list[str]:
        """本节点已知的全部节点名称（汇聚点 = 所有接入节点 + 自身）。"""
        ...

    def relay(
        self,
        event: str,
        *,
        direction: str = "out",
        mode: str = "broadcast",
        node: str | None = None,
        topic: str | None = None,
    ) -> Callable[[], None]:
        """跨节点事件转发：``out`` 本地事件 → 网络；``in`` 网络 → 本地事件。

        线上 topic 约定 ``relay/{event}``（决策 D5）；注册绑调用方 fiber。
        """
        ...

    def status(self, message_id: str) -> MessageStatus | None:
        """单条消息投递状态；未知 id 返回 ``None``。"""
        ...

    async def flush(self, timeout: float = 5.0) -> None:
        """排空在途消息（测试 / 关停）；超时返回。"""
        ...


__all__: list[str] = [
    "AT_LEAST_ONCE",
    "FIRE_AND_FORGET",
    "CommInterface",
    "CommMeta",
    "CommTopicHandler",
    "CommTransport",
    "DeliveryPolicy",
    "Envelope",
    "MessageState",
    "MessageStatus",
    "TransportResult",
    "bounded_policy",
    "resolve_policy",
]
