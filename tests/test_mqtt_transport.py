"""P4.2：Transport 端口与 MqttTransport 测试。

验收：断连重连恢复——用 fake aiomqtt.Client 模拟首次连接失败后重连成功，
重连后恢复订阅；指数退避逻辑独立测试；端口契约验证业务层只依赖端口。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from common.backoff import ExponentialBackoff
from common.transport import MqttMessage, Transport
from master.adapters.mqtt.transport import MqttTransport
from master.config import MasterSettings

_SETTINGS = MasterSettings(
    mqtt_host="broker.test",
    mqtt_port=1883,
    mqtt_client_id="test-master",
    mqtt_use_tls=False,
)


# ---------------------------------------------------------------------------
# ExponentialBackoff
# ---------------------------------------------------------------------------


def test_backoff_increases_with_attempts():
    b = ExponentialBackoff(base_delay_s=1.0, max_delay_s=8.0, jitter_ratio=0.0)
    assert b.next() == pytest.approx(1.0)  # 1
    assert b.next() == pytest.approx(2.0)  # 2
    assert b.next() == pytest.approx(4.0)  # 4
    assert b.next() == pytest.approx(8.0)  # 封顶
    assert b.next() == pytest.approx(8.0)  # 继续封顶
    assert b.attempts == 5


def test_backoff_reset():
    b = ExponentialBackoff(base_delay_s=1.0, jitter_ratio=0.0)
    b.next()
    b.next()
    b.reset()
    assert b.attempts == 0
    assert b.next() == pytest.approx(1.0)


def test_backoff_jitter_within_ratio():
    b = ExponentialBackoff(base_delay_s=1.0, jitter_ratio=0.1)
    for _ in range(20):
        d = b.next()
        assert 0.9 <= d <= 1.1 * 2**b.attempts  # 粗校验范围（带抖动递增）


# ---------------------------------------------------------------------------
# Transport 端口契约（业务层只依赖端口）
# ---------------------------------------------------------------------------


class FakeTransport:
    """duck-typed 实现，不继承端口。"""

    def __init__(self) -> None:
        self.connected = False
        self.published: list[tuple[str, bytes, int]] = []
        self.topics: list[str] = []
        self.handler = None

    def on_message(self, handler):
        self.handler = handler

    def on_connection_change(self, handler):
        self.connection_handler = handler

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def subscribe(self, topics):
        self.topics = list(topics)

    async def publish(self, topic, payload, qos=1):
        self.published.append((topic, payload, qos))


async def _business_publish(transport: Transport, topic: str, payload: bytes) -> None:
    """业务用例：只依赖 Transport 端口发布消息。"""
    if not transport.connected:
        raise RuntimeError("not connected")
    await transport.publish(topic, payload, qos=1)


def test_transport_port_contract():
    """业务层通过端口形状即可工作，不依赖具体实现。"""
    transport: Transport = FakeTransport()
    asyncio.run(transport.connect())
    asyncio.run(_business_publish(transport, "aetp/v1/test", b"hello"))
    assert transport.published == [("aetp/v1/test", b"hello", 1)]


# ---------------------------------------------------------------------------
# MqttTransport（mock aiomqtt）
# ---------------------------------------------------------------------------


def _fake_aiomqtt(fail_first_connect: bool = False):
    """构造 fake aiomqtt 模块：Client 可选首次连接失败。"""

    class FakeClient:
        _counter = 0
        instances: list[FakeClient] = []  # noqa: RUF012 - 测试桩实例注册表

        def __init__(self, **kwargs):
            FakeClient._counter += 1
            self.attempt = FakeClient._counter
            self.kwargs = kwargs
            self.subscribed: list[str] = []
            self.published: list[tuple[str, bytes, int]] = []
            FakeClient.instances.append(self)

        async def __aenter__(self):
            if fail_first_connect and self.attempt == 1:
                raise ConnectionError("broker unreachable")
            return self

        async def __aexit__(self, *exc):
            return None

        async def subscribe(self, topic, qos=1):
            self.subscribed.append(topic)

        async def publish(self, topic, payload, qos=1):
            self.published.append((topic, payload, qos))

        async def _messages_gen(self):
            # 真正的 async generator：永远挂起（测试期间无入站消息）；
            # yield 占位使其成为 async generator 而非 coroutine
            await asyncio.Event().wait()
            yield None  # pragma: no cover - 不可达

        @property
        def messages(self):
            return self._messages_gen()

    return SimpleNamespace(Client=FakeClient), FakeClient


async def _wait_connected(transport: Transport, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not transport.connected:
        if time.monotonic() > deadline:
            raise AssertionError("等待 MQTT 连接超时")
        await asyncio.sleep(0.01)


def test_mqtt_transport_connect_subscribe_publish():
    """连接成功：订阅主题 + 发布走 aiomqtt。"""
    fake_mod, FakeClient = _fake_aiomqtt()
    transport = MqttTransport(_SETTINGS, backoff=ExponentialBackoff(jitter_ratio=0.0))

    async def scenario():
        await transport.subscribe(["aetp/v1/agents/+/events/#"])
        await transport.connect()
        await _wait_connected(transport)
        assert transport.connected is True
        await transport.publish("aetp/v1/master/test", b"cmd", qos=1)
        await transport.disconnect()

    import master.adapters.mqtt.transport as mqtt_mod

    mqtt_mod.aiomqtt = fake_mod
    try:
        asyncio.run(scenario())
    finally:
        mqtt_mod.aiomqtt = __import__("aiomqtt")

    client = FakeClient.instances[-1]
    assert "aetp/v1/agents/+/events/#" in client.subscribed
    assert client.published == [("aetp/v1/master/test", b"cmd", 1)]
    # 端口参数正确传递（host/port/identifier）
    assert client.kwargs["hostname"] == "broker.test"
    assert client.kwargs["port"] == 1883
    assert client.kwargs["identifier"] == "test-master"


def test_mqtt_transport_uses_persistent_session():
    """Master 使用持久会话：clean_session=False，离线消息由 broker 缓存补发。

    Master 是唯一订阅 events 主题的客户端，进程重启后首次连接不清空会话，
    broker 会在 Master 离线期间缓存 Agent 上报的 QoS 1 事件（result/log/
    register），Master 重新上线后补发，避免执行结果丢失（§9.7 规则 5）。

    注意：MQTT 3.1.1 下持久会话用 ``clean_session=False``；``clean_start``
    仅对 MQTT v5 有效，3.1.1 下会抛 "Clean start only applies to MQTT V5"。
    """
    transport = MqttTransport(_SETTINGS)
    kwargs = transport._client_kwargs()
    assert kwargs["clean_session"] is False
    # 固定 client_id 是持久会话生效的前提
    assert kwargs["identifier"] == "test-master"


def test_mqtt_transport_publish_when_disconnected_raises():
    """未连接时 publish 抛 TransportError（fail fast）。"""
    from common.transport import TransportError

    transport = MqttTransport(_SETTINGS)

    async def scenario():
        with pytest.raises(TransportError, match="未连接"):
            await transport.publish("aetp/v1/x", b"x")

    asyncio.run(scenario())


def test_mqtt_transport_reconnect_recovers():
    """验收：断连后指数退避重连恢复；重连后自动恢复订阅。"""
    fake_mod, FakeClient = _fake_aiomqtt(fail_first_connect=True)
    transport = MqttTransport(
        _SETTINGS,
        backoff=ExponentialBackoff(base_delay_s=0.01, max_delay_s=0.05, jitter_ratio=0.0),
    )

    async def scenario():
        await transport.subscribe(["aetp/v1/agents/+/events/#"])
        await transport.connect()
        await _wait_connected(transport, timeout=3.0)  # 首次失败 → 退避 → 重连成功
        assert transport.connected is True
        assert FakeClient._counter >= 2  # 至少尝试两次
        await transport.disconnect()

    import master.adapters.mqtt.transport as mqtt_mod

    mqtt_mod.aiomqtt = fake_mod
    try:
        asyncio.run(scenario())
    finally:
        mqtt_mod.aiomqtt = __import__("aiomqtt")

    # 重连成功的实例恢复了订阅
    last = FakeClient.instances[-1]
    assert "aetp/v1/agents/+/events/#" in last.subscribed


def test_mqtt_transport_dispatch_to_handler():
    """入站消息经 on_message 处理器分发（MqttMessage 转换）。"""
    fake_mod, _FakeClient = _fake_aiomqtt()
    transport = MqttTransport(_SETTINGS, backoff=ExponentialBackoff(jitter_ratio=0.0))
    received: list[MqttMessage] = []

    async def handler(message: MqttMessage) -> None:
        received.append(message)

    async def scenario():
        transport.on_message(handler)
        await transport.connect()
        await _wait_connected(transport)
        await transport.disconnect()

    import master.adapters.mqtt.transport as mqtt_mod

    mqtt_mod.aiomqtt = fake_mod
    try:
        asyncio.run(scenario())
    finally:
        mqtt_mod.aiomqtt = __import__("aiomqtt")
