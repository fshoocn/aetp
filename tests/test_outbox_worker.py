"""P4.3 Outbox worker 测试（fake transport + fake uow）。

验收（§15.3 P4.3）：断连后 outbox 恢复发送；发送成功标记 succeeded；
失败按指数退避推进并最终 exhausted；单条失败不影响同批其余消息。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Self, cast

from common.backoff import ExponentialBackoff
from common.transport import TransportError
from master.domain.enums import OutboxStatus
from master.domain.models import OutboxMessage
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow
from master.workers.outbox_worker import OutboxWorker

# -- fakes ----------------------------------------------------------------


class FakeOutboxRepo:
    """内存 outbox 仓储（与 SQLAlchemy 实现同语义的事务性 claim）。"""

    def __init__(self, messages: list[OutboxMessage] | None = None) -> None:
        self._messages: dict[str, OutboxMessage] = {m.outbox_id: m for m in (messages or [])}
        self.claimed: list[OutboxMessage] = []
        self.updated: list[OutboxMessage] = []

    def claim_due(self, *, limit: int = 100, now=None) -> list[OutboxMessage]:
        now = now or utcnow()
        due = [
            m
            for m in self._messages.values()
            if m.status in (OutboxStatus.PENDING, OutboxStatus.RETRYING)
            and (m.next_attempt_at is None or m.next_attempt_at <= now)
        ][:limit]
        for m in due:
            m.status = OutboxStatus.SENDING
            m.attempts += 1
            m.sent_at = now
            self.claimed.append(m)
        return list(due)

    def update(self, message: OutboxMessage) -> OutboxMessage:
        self._messages[message.outbox_id] = message
        self.updated.append(message)
        return message


class FakeUoW:
    def __init__(self, repo: FakeOutboxRepo) -> None:
        self.outbox_messages = repo

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None


class FakeTransport:
    """记录发布；可对指定 topic 或整体失败。完整实现 Transport 协议。"""

    def __init__(self, fail_topics: tuple[str, ...] = (), fail_all: bool = False) -> None:
        self.fail_topics = set(fail_topics)
        self.fail_all = fail_all
        self.published: list[tuple[str, bytes, int]] = []
        self._handler = None

    @property
    def connected(self) -> bool:
        return True

    def on_message(self, handler) -> None:
        self._handler = handler

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def subscribe(self, topics: list[str]) -> None:
        return None

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        if self.fail_all or topic in self.fail_topics:
            raise TransportError(f"MQTT 未连接，无法发布: {topic}")
        self.published.append((topic, payload, qos))


def _msg(
    outbox_id: str = "m1",
    topic: str = "aetp/v1/master/agents/bench-001/commands/assign",
    status: OutboxStatus = OutboxStatus.PENDING,
    attempts: int = 0,
    next_at=None,
    payload: dict | None = None,
) -> OutboxMessage:
    return OutboxMessage(
        outbox_id=outbox_id,
        aggregate_type="task_run",
        aggregate_id="run-1",
        topic=topic,
        payload=payload or {"message_type": "run.assign", "run_id": "run-1"},
        qos=1,
        status=status,
        attempts=attempts,
        next_attempt_at=next_at,
    )


def _make_worker(repo: FakeOutboxRepo, transport: FakeTransport, **kwargs) -> OutboxWorker:
    # FakeUoW 是鸭子类型（含 outbox_messages + 上下文管理），不继承 UnitOfWork ABC；
    # 显式 cast 为 UoW 工厂以满足 worker 的 Callable[[], UnitOfWork] 签名
    factory = cast(Callable[[], UnitOfWork], lambda: FakeUoW(repo))
    return OutboxWorker(factory, transport, **kwargs)


# -- tests ----------------------------------------------------------------


def test_outbox_worker_publishes_and_marks_succeeded():
    """发送成功：publish 到正确 topic、payload 为 JSON bytes、标记 succeeded。"""
    repo = FakeOutboxRepo([_msg()])
    transport = FakeTransport()
    worker = _make_worker(repo, transport)

    async def scenario():
        assert await worker.run_once() == 1

    asyncio.run(scenario())

    assert len(transport.published) == 1
    topic, payload, qos = transport.published[0]
    assert topic == "aetp/v1/master/agents/bench-001/commands/assign"
    assert qos == 1
    assert json.loads(payload) == {"message_type": "run.assign", "run_id": "run-1"}

    sent = repo.updated[-1]
    assert sent.status is OutboxStatus.SUCCEEDED
    assert sent.sent_at is not None


def test_outbox_worker_failure_marks_retrying_with_backoff():
    """发送失败：标记 retrying，next_attempt_at 按指数退避推进。"""
    repo = FakeOutboxRepo([_msg()])
    transport = FakeTransport(fail_all=True)
    backoff = ExponentialBackoff(base_delay_s=1.0, max_delay_s=10.0, jitter_ratio=0.0)
    worker = _make_worker(repo, transport, retry_backoff=backoff, max_attempts=5)

    before = utcnow()

    async def scenario():
        await worker.run_once()

    asyncio.run(scenario())

    sent = repo.updated[-1]
    assert sent.status is OutboxStatus.RETRYING
    assert sent.attempts == 1  # claim 时已 +1
    assert sent.next_attempt_at is not None
    # base_delay=1.0（jitter=0）→ 约 now + 1s
    assert sent.next_attempt_at >= before + timedelta(seconds=0.99)
    assert sent.next_attempt_at <= before + timedelta(seconds=1.1)


def test_outbox_worker_exhausts_after_max_attempts():
    """达到 max_attempts：标记 exhausted 且不再推进 next_attempt_at。"""
    repo = FakeOutboxRepo([_msg(attempts=2)])  # claim 后 attempts=3
    transport = FakeTransport(fail_all=True)
    worker = _make_worker(repo, transport, max_attempts=3)

    async def scenario():
        await worker.run_once()

    asyncio.run(scenario())

    sent = repo.updated[-1]
    assert sent.status is OutboxStatus.EXHAUSTED
    assert sent.attempts == 3
    assert sent.next_attempt_at is None


def test_outbox_worker_disconnected_transport_marks_retrying():
    """未连接（TransportError）：标记 retrying，断连恢复后继续发送。"""
    repo = FakeOutboxRepo([_msg()])
    transport = FakeTransport(fail_all=True)
    worker = _make_worker(repo, transport, max_attempts=2)

    async def scenario():
        await worker.run_once()

    asyncio.run(scenario())
    assert repo.updated[-1].status is OutboxStatus.RETRYING

    # 断连恢复：同一条消息再次到期 → 发送成功
    transport.fail_all = False
    msg = repo.updated[-1]
    msg.status = OutboxStatus.RETRYING
    msg.next_attempt_at = utcnow() - timedelta(seconds=1)
    repo._messages[msg.outbox_id] = msg

    async def scenario2():
        await worker.run_once()

    asyncio.run(scenario2())
    assert transport.published
    assert repo.updated[-1].status is OutboxStatus.SUCCEEDED


def test_outbox_worker_isolates_message_failures():
    """单条失败不影响同批其余消息（fail-open）。"""
    repo = FakeOutboxRepo(
        [
            _msg(outbox_id="good", topic="aetp/v1/master/ok"),
            _msg(outbox_id="bad", topic="aetp/v1/master/bad"),
        ]
    )
    transport = FakeTransport(fail_topics=("aetp/v1/master/bad",))
    worker = _make_worker(repo, transport)

    async def scenario():
        assert await worker.run_once() == 2

    asyncio.run(scenario())

    by_id = {m.outbox_id: m for m in repo.updated}
    assert by_id["good"].status is OutboxStatus.SUCCEEDED
    assert by_id["bad"].status is OutboxStatus.RETRYING
    assert [t for t, _, _ in transport.published] == ["aetp/v1/master/ok"]


def test_outbox_worker_background_loop_polls_and_sends():
    """后台循环：start 后周期轮询，新入队消息被自动发送，stop 停止。"""
    repo = FakeOutboxRepo()
    transport = FakeTransport()
    worker = _make_worker(repo, transport, poll_interval_s=0.01, batch_size=10)

    async def scenario():
        await worker.start()
        # 入队一条待发送消息
        repo._messages["bg-1"] = _msg(outbox_id="bg-1", topic="aetp/v1/master/bg")
        for _ in range(200):
            if any(m.status is OutboxStatus.SUCCEEDED for m in repo.updated):
                break
            await asyncio.sleep(0.01)
        await worker.stop()
        assert transport.published, "后台循环应已发送入队消息"
        assert repo.updated[-1].status is OutboxStatus.SUCCEEDED

    asyncio.run(scenario())


def test_outbox_worker_run_once_returns_processed_count():
    """run_once 返回本次处理的条数；无到期消息返回 0。"""
    repo = FakeOutboxRepo([_msg()])
    transport = FakeTransport()
    worker = _make_worker(repo, transport)

    async def scenario():
        assert await worker.run_once() == 1
        assert await worker.run_once() == 0  # 已发送，不再重复取

    asyncio.run(scenario())
    assert len(transport.published) == 1
