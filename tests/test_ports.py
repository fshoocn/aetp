"""P3.7：ports 端口契约测试。

核心验收：业务层只依赖端口（EventStore/Hook/Notification），不依赖任何
adapter；测试用 duck-typed fake 实现注入业务用例，验证端口形状可用。
"""

from __future__ import annotations

import asyncio

from master.domain.event_store import EventStore
from master.domain.hooks import (
    AdmissionHook,
    EventHook,
    HookContext,
    HookDecision,
)
from master.domain.models import DomainEvent
from master.domain.notifications import (
    DeliveryReceipt,
    NotificationEndpoint,
    NotificationMessage,
    NotificationSender,
    SecretStore,
    SecretValue,
)

# ---------------------------------------------------------------------------
# duck-typed fake adapter（不继承端口，纯形状匹配）
# ---------------------------------------------------------------------------


class FakeEventStore:
    def __init__(self) -> None:
        self._events: list[DomainEvent] = []

    def append(self, event: DomainEvent) -> DomainEvent:
        event.sequence = len(self._events) + 1  # 模拟真实 EventStore 分配单调 sequence
        self._events.append(event)
        return event

    def read(
        self,
        *,
        project_id: str | None = None,
        after_sequence: int | None = None,
        limit: int = 100,
    ) -> list[DomainEvent]:
        events = self._events
        if project_id is not None:
            events = [e for e in events if e.project_id == project_id]
        if after_sequence is not None:
            events = [e for e in events if (e.sequence or 0) > after_sequence]
        return events[-limit:]

    def get_by_event_id(self, event_id: str) -> DomainEvent | None:
        return next((e for e in self._events if e.event_id == event_id), None)


class AllowAllHook:
    name = "allow_all"
    stage = "run.before_dispatch"
    order = 10

    async def evaluate(self, context: HookContext) -> HookDecision:
        return HookDecision(allowed=True)


class DenyHook:
    name = "deny_all"
    stage = "run.before_dispatch"
    order = 20

    async def evaluate(self, context: HookContext) -> HookDecision:
        return HookDecision(allowed=False, reason="policy denied", code="POLICY_DENIED")


class RunEventHook:
    name = "run_notifier"
    event_types = frozenset({"run.succeeded", "run.failed"})

    def __init__(self) -> None:
        self.handled: list[str] = []

    async def handle(self, event: DomainEvent) -> None:
        self.handled.append(event.event_type)


class ConsoleSender:
    channel_type = "console_test"

    def __init__(self) -> None:
        self.sent: list[tuple[NotificationMessage, NotificationEndpoint]] = []

    async def send(self, message: NotificationMessage, endpoint: NotificationEndpoint) -> DeliveryReceipt:
        self.sent.append((message, endpoint))
        return DeliveryReceipt(status="succeeded", detail="console")


class InMemorySecretStore:
    def __init__(self, secret: str = "s3cret") -> None:
        self._secret = secret

    def get(self, secret_ref: str) -> SecretValue:
        return SecretValue(value=self._secret)


# ---------------------------------------------------------------------------
# 业务用例：只依赖端口（不 import 任何 adapter）
# ---------------------------------------------------------------------------


async def _notify_run_terminal_events(
    store: EventStore,
    sender: NotificationSender,
    secrets: SecretStore,
) -> list[DeliveryReceipt]:
    """业务用例：读取终态 Run 事件 → 组装通知 → 经端口投递。

    只依赖 EventStore / NotificationSender / SecretStore 端口形状，
    不依赖任何具体实现。
    """
    receipts: list[DeliveryReceipt] = []
    endpoint = NotificationEndpoint(
        endpoint_id="ep-1",
        channel_type="console_test",
        name="console",
        secret_ref="ref/console",
    )
    for event in store.read():
        if endpoint.secret_ref is not None:
            secrets.get(endpoint.secret_ref)  # 演示：密钥经 SecretStore 取回，不落明文
        if event.event_type in {"run.succeeded", "run.failed"}:
            message = NotificationMessage(
                subject=event.event_type,
                body=f"run {event.aggregate_id} -> {event.event_type}",
                severity="info",
                event=event,
            )
            receipts.append(await sender.send(message, endpoint))
    return receipts


async def _run_admission_pipeline(hooks: list[AdmissionHook], context: HookContext) -> bool:
    """业务用例：准入 Hook 链按 (order, name) 排序评估，全部 allowed 才放行。"""
    ordered = sorted(hooks, key=lambda h: (h.order, h.name))
    for hook in ordered:
        decision = await hook.evaluate(context)
        if not decision.allowed:
            return False
    return True


# ---------------------------------------------------------------------------
# 端口契约测试
# ---------------------------------------------------------------------------


def test_event_store_port_contract():
    store: EventStore = FakeEventStore()  # 端口类型注解，duck-typed 注入
    store.append(DomainEvent(event_id="E-1", project_id="p1", event_type="run.created", aggregate_id="R-1"))
    store.append(DomainEvent(event_id="E-2", project_id="p1", event_type="run.succeeded", aggregate_id="R-1"))
    assert store.get_by_event_id("E-2") is not None
    assert [e.event_id for e in store.read(after_sequence=0)] == ["E-1", "E-2"]


def test_admission_hook_port_contract():
    hook: AdmissionHook = AllowAllHook()
    assert hook.name == "allow_all"
    assert hook.stage == "run.before_dispatch"
    decision = asyncio.run(hook.evaluate(HookContext(stage=hook.stage)))
    assert decision.allowed is True


def test_event_hook_port_contract():
    hook: EventHook = RunEventHook()
    assert hook.event_types == frozenset({"run.succeeded", "run.failed"})
    asyncio.run(hook.handle(DomainEvent(event_id="E-1", project_id="p1", event_type="run.failed", aggregate_id="R-1")))
    assert hook.handled == ["run.failed"]


def test_notification_sender_and_secret_store_port_contract():
    sender: NotificationSender = ConsoleSender()
    secrets: SecretStore = InMemorySecretStore()
    endpoint = NotificationEndpoint(endpoint_id="ep-1", channel_type="console_test", name="console", secret_ref="ref/1")
    receipt = asyncio.run(
        sender.send(
            NotificationMessage(subject="s", body="b"),
            endpoint,
        )
    )
    assert receipt.status == "succeeded"
    assert secrets.get("ref/1").value == "s3cret"


# ---------------------------------------------------------------------------
# 业务层不依赖 adapter（P3.7 核心验收）
# ---------------------------------------------------------------------------


def test_business_logic_depends_on_ports_not_adapters():
    """业务用例注入 duck-typed fake，验证只依赖端口形状即可工作。"""
    store = FakeEventStore()
    store.append(DomainEvent(event_id="E-1", project_id="p1", event_type="run.created", aggregate_id="R-1"))
    store.append(DomainEvent(event_id="E-2", project_id="p1", event_type="run.failed", aggregate_id="R-1"))
    sender = ConsoleSender()
    secrets = InMemorySecretStore()

    receipts = asyncio.run(_notify_run_terminal_events(store, sender, secrets))

    assert len(receipts) == 1
    assert receipts[0].status == "succeeded"
    assert len(sender.sent) == 1
    message, endpoint = sender.sent[0]
    assert message.subject == "run.failed"
    assert message.event is not None and message.event.event_id == "E-2"
    assert endpoint.channel_type == "console_test"


def test_admission_pipeline_order_and_deny():
    """准入 Hook 按 (order, name) 稳定排序；任一 deny 即拒绝（fail closed）。"""
    allow = AllowAllHook()  # order 10
    deny = DenyHook()  # order 20
    context = HookContext(stage="run.before_dispatch")

    allowed = asyncio.run(_run_admission_pipeline([deny, allow], context))
    assert allowed is False  # deny hook 阻止

    allowed = asyncio.run(_run_admission_pipeline([allow], context))
    assert allowed is True
