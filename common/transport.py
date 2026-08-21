"""Transport 端口（P4.2，Master/Agent 共享）。

业务层只依赖本端口（connect/subscribe/publish/on_message），
不依赖具体 MQTT 客户端（aiomqtt 等）。Master 与 Agent 共用同一契约；
实现见各组件 adapters/mqtt/transport.py。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class TransportError(Exception):
    """传输层错误（未连接/发布失败等）。"""


@dataclass(frozen=True)
class MqttMessage:
    """到达的 MQTT 消息。"""

    # sym:topic 消息主题
    topic: str
    # sym:payload 消息载荷（bytes）
    payload: bytes
    # sym:qos 到达 QoS
    qos: int = 1


class MessageHandler(Protocol):
    """消息处理器（订阅方注册）。"""

    async def __call__(self, message: MqttMessage) -> None: ...


class ConnectionHandler(Protocol):
    """连接状态变化处理器（connected, session_id）。"""

    async def __call__(self, connected: bool, session_id: str | None = None) -> None: ...


class Transport(Protocol):
    """MQTT 传输端口。

    约定：
    - connect() 启动后台连接/重连循环（指数退避，§9.7 规则 5）
    - subscribe() 更新订阅集合；已连接时立即订阅，重连后自动恢复订阅
    - publish() 在未连接时抛 TransportError
    - on_message() 注册消息处理器（单处理器，由上层按主题分发）
    """

    @property
    def connected(self) -> bool:
        """当前是否已连接。"""
        ...

    def on_message(self, handler: MessageHandler) -> None:
        """注册入站消息处理器。"""
        ...

    def on_connection_change(self, handler: ConnectionHandler) -> None:
        """注册连接状态变化处理器。"""
        ...

    async def connect(self) -> None:
        """启动连接与重连循环（幂等）。"""
        ...

    async def disconnect(self) -> None:
        """停止连接循环并释放资源（幂等）。"""
        ...

    async def subscribe(self, topics: list[str]) -> None:
        """订阅主题集合（更新订阅并即时生效）。"""
        ...

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        """发布消息；未连接抛 TransportError。"""
        ...
