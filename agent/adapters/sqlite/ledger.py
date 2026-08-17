"""Agent 本地 SQLite 账本实现（P5.2，§9.3）。

``SQLiteLedger`` 实现 ``agent.domain.ledger.Ledger`` 端口，承载 Agent 本地
五张表：agent_runs / agent_inbox_messages / agent_outbox_messages /
agent_task_log_spool / agent_script_cache。

关键点：

- **原子 claim**：``agent_runs`` 以 ``run_id`` 为主键，claim 用 SQLite 的
  ``INSERT ... ON CONFLICT DO NOTHING`` 保证并发下同一 run 只被成功插入一次；
  插入失败时再区分“同一 attempt 重复派发”（返回 False，不二次执行）与
  “同一 run 的新 attempt”（D-20 failover，更新 attempt_no 后返回 True）。
- **入站去重**：``(origin_id, message_id)`` 唯一约束 + 冲突忽略。
- 所有状态列存枚举的 ``.value``（小写字符串），读取时还原为枚举，
  杜绝魔法字符串。

时间统一存 naive UTC（SQLite 本地单机语义）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    delete,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from agent.domain.enums import AgentOutboxStatus, AgentRunStatus
from agent.domain.ledger import (
    AgentInboxEntry,
    AgentOutboxEntry,
    AgentRun,
    Ledger,
    ScriptCacheEntry,
    TaskLogSpoolEntry,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """naive UTC（SQLite 本地账本统一存 naive UTC）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _Base(DeclarativeBase):
    """Agent 本地账本 ORM 基类。"""


class AgentRunORM(_Base):
    """run claim 与执行状态表（run_id 主键）。"""

    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class AgentInboxORM(_Base):
    """入站命令去重表。"""

    __tablename__ = "agent_inbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "origin_id", "message_id", name="uq_agent_inbox_origin_message"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    origin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class AgentOutboxORM(_Base):
    """可靠出站消息表（注册/ACK/结果）。"""

    __tablename__ = "agent_outbox_messages"

    outbox_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AgentOutboxStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 已发送（sent）的消息无需下次重试时间，故可空
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=_utcnow
    )
    # sending 状态的租约到期后可被另一个 worker 回收
    claimed_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class TaskLogSpoolORM(_Base):
    """任务日志本地缓冲表。"""

    __tablename__ = "agent_task_log_spool"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_log_run_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(String(8192), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class ScriptCacheORM(_Base):
    """脚本包本地缓存引用表。"""

    __tablename__ = "agent_script_cache"
    __table_args__ = (
        UniqueConstraint(
            "script_id", "version", "sha256",
            name="uq_agent_cache_script_version_sha",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    script_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class SQLiteLedger:
    """Agent 本地账本的 SQLite 实现（每个方法独立短事务/会话）。"""

    def __init__(self, url: str, *, max_spool_bytes: int = 104857600) -> None:
        if max_spool_bytes <= 0:
            raise ValueError("max_spool_bytes 必须大于 0")
        self._engine: Engine = create_engine(url)
        _Base.metadata.create_all(self._engine)
        # expire_on_commit=False：提交后属性仍可用，便于在会话外把 ORM 行
        # 转换为领域对象而不触发 DetachedInstanceError
        self._session_factory = sessionmaker(
            bind=self._engine, expire_on_commit=False
        )
        self._max_spool_bytes = max_spool_bytes

    # ---- agent_runs：原子 claim ----

    def claim_run(self, run_id: str, attempt_no: int) -> bool:
        """原子 claim：新 run 或新 attempt 返回 True；重复派发返回 False。

        用 ``ON CONFLICT DO NOTHING`` 保证“先插入者胜”：插入成功即首次
        claim；冲突时读出现有 attempt_no，相同视为重复派发，不同则按
        D-20 更新为新的 attempt。
        """
        now = _utcnow()
        with self._session_factory.begin() as session:
            inserted = session.execute(
                sqlite_insert(AgentRunORM)
                .values(
                    run_id=run_id,
                    attempt_no=attempt_no,
                    status=AgentRunStatus.CLAIMED.value,
                    cancelled=False,
                    result_summary={},
                    claimed_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["run_id"])
            )
            if inserted.rowcount == 1:
                return True
            existing = session.execute(
                select(AgentRunORM).where(AgentRunORM.run_id == run_id)
            ).scalar_one()
            if attempt_no <= existing.attempt_no:
                return False  # 迟到旧 attempt 或同 attempt 重复派发
            if existing.status in {
                AgentRunStatus.SUCCEEDED.value,
                AgentRunStatus.CANCELLED.value,
            }:
                return False  # 已成功/取消的 Run 不允许被旧链路重新抢占
            if existing.status not in {
                AgentRunStatus.FAILED.value,
                AgentRunStatus.TIMED_OUT.value,
            }:
                return False  # 当前 attempt 尚未失败/超时，不接受未来 attempt
            # 同一 run 的新 attempt（D-20 failover）
            session.execute(
                update(AgentRunORM)
                .where(AgentRunORM.run_id == run_id)
                .values(
                    attempt_no=attempt_no,
                    status=AgentRunStatus.CLAIMED.value,
                    cancelled=False,
                    updated_at=now,
                )
            )
            return True

    def get_run(self, run_id: str) -> AgentRun | None:
        """读取本地 Run 状态；不存在返回 None。"""
        with self._session_factory.begin() as session:
            row = session.execute(
                select(AgentRunORM).where(AgentRunORM.run_id == run_id)
            ).scalar_one_or_none()
        return _to_run(row) if row is not None else None

    def update_run(self, run: AgentRun) -> None:
        """更新 Run 状态/取消标志/结果摘要。"""
        with self._session_factory.begin() as session:
            session.execute(
                update(AgentRunORM)
                .where(AgentRunORM.run_id == run.run_id)
                .values(
                    attempt_no=run.attempt_no,
                    status=run.status.value,
                    cancelled=run.cancelled,
                    result_summary=run.result_summary,
                    updated_at=_utcnow(),
                )
            )

    def list_active_runs(self) -> list[AgentRun]:
        """返回未终结的 Run（claimed/running），用于心跳负载与恢复现场。"""
        active = {
            AgentRunStatus.CLAIMED.value,
            AgentRunStatus.RUNNING.value,
        }
        with self._session_factory.begin() as session:
            rows = session.execute(
                select(AgentRunORM)
                .where(AgentRunORM.status.in_(active))
                .order_by(AgentRunORM.run_id)
            ).scalars().all()
        return [_to_run(row) for row in rows]

    # ---- inbox 去重 ----

    def record_inbox(
        self, origin_id: str, message_id: str, message_type: str
    ) -> bool:
        """入站去重：首次记录返回 True，已存在返回 False。"""
        with self._session_factory.begin() as session:
            result = session.execute(
                sqlite_insert(AgentInboxORM)
                .values(
                    origin_id=origin_id,
                    message_id=message_id,
                    message_type=message_type,
                    received_at=_utcnow(),
                )
                .on_conflict_do_nothing(
                    index_elements=["origin_id", "message_id"]
                )
            )
            return result.rowcount == 1

    # ---- outbox ----

    def enqueue_outbox(self, outbox_id: str, topic: str, payload: dict) -> None:
        """写入一条待发送出站消息（幂等：同 outbox_id 忽略）。"""
        with self._session_factory.begin() as session:
            session.execute(
                sqlite_insert(AgentOutboxORM)
                .values(
                    outbox_id=outbox_id,
                    topic=topic,
                    payload=payload,
                    status=AgentOutboxStatus.PENDING.value,
                    attempts=0,
                    next_attempt_at=_utcnow(),
                )
                .on_conflict_do_nothing(index_elements=["outbox_id"])
            )

    def replace_outbox(self, outbox_id: str, topic: str, payload: dict) -> None:
        """替换一条可重放消息，并重置发送状态与租约。"""
        with self._session_factory.begin() as session:
            session.execute(
                sqlite_insert(AgentOutboxORM)
                .values(
                    outbox_id=outbox_id,
                    topic=topic,
                    payload=payload,
                    status=AgentOutboxStatus.PENDING.value,
                    attempts=0,
                    next_attempt_at=_utcnow(),
                    claimed_until=None,
                )
                .on_conflict_do_update(
                    index_elements=["outbox_id"],
                    set_={
                        "topic": topic,
                        "payload": payload,
                        "status": AgentOutboxStatus.PENDING.value,
                        "attempts": 0,
                        "next_attempt_at": _utcnow(),
                        "claimed_until": None,
                    },
                )
            )

    def claim_due_outbox(
        self, limit: int, now: datetime
    ) -> list[AgentOutboxEntry]:
        """事务性领取到期待发送消息，并设置短租约。

        先回收租约过期的 ``sending`` 消息，再用带子查询的 UPDATE 把
        ``pending`` 原子改成 ``sending``，最后只返回本次实际领取的行。
        这样多个 worker 并发领取时不会重复拿到同一消息。
        """
        lease_until = now + timedelta(seconds=30)
        with self._session_factory.begin() as session:
            session.execute(
                update(AgentOutboxORM)
                .where(
                    AgentOutboxORM.status == AgentOutboxStatus.SENDING.value,
                    AgentOutboxORM.claimed_until <= now,
                )
                .values(
                    status=AgentOutboxStatus.PENDING.value,
                    next_attempt_at=now,
                    claimed_until=None,
                )
            )
            candidate_ids = select(AgentOutboxORM.outbox_id).where(
                AgentOutboxORM.status == AgentOutboxStatus.PENDING.value,
                AgentOutboxORM.next_attempt_at <= now,
            ).order_by(AgentOutboxORM.created_at).limit(limit)
            claimed = session.execute(
                update(AgentOutboxORM)
                .where(
                    AgentOutboxORM.outbox_id.in_(candidate_ids),
                    AgentOutboxORM.status == AgentOutboxStatus.PENDING.value,
                )
                .values(
                    status=AgentOutboxStatus.SENDING.value,
                    claimed_until=lease_until,
                )
                .returning(AgentOutboxORM.outbox_id)
            ).scalars().all()
            if not claimed:
                return []
            rows = session.execute(
                select(AgentOutboxORM).where(
                    AgentOutboxORM.outbox_id.in_(claimed)
                )
            ).scalars().all()
        return [_to_outbox(row) for row in rows]

    def mark_outbox(
        self,
        outbox_id: str,
        *,
        status: AgentOutboxStatus,
        attempts: int,
        next_attempt_at: datetime | None,
        claimed_until: datetime | None = None,
    ) -> None:
        """更新出站消息发送状态与下次重试时间。"""
        with self._session_factory.begin() as session:
            session.execute(
                update(AgentOutboxORM)
                .where(AgentOutboxORM.outbox_id == outbox_id)
                .values(
                    status=status.value,
                    attempts=attempts,
                    next_attempt_at=next_attempt_at,
                    claimed_until=claimed_until,
                )
            )

    # ---- 任务日志 spool ----

    def append_task_log(self, entry: TaskLogSpoolEntry) -> None:
        """追加一条任务日志，并按字节上限淘汰低等级未发布日志。

        ``error`` 日志永不因容量策略被删除；如果单条 error 本身超过上限，
        仍保留该条并记录告警。debug/info/warn 则从最早未发布条目开始淘汰。
        """
        with self._session_factory.begin() as session:
            duplicate = session.execute(
                select(TaskLogSpoolORM.id).where(
                    TaskLogSpoolORM.run_id == entry.run_id,
                    TaskLogSpoolORM.sequence == entry.sequence,
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                return
            detail_size = len(
                json.dumps(entry.detail, ensure_ascii=False).encode("utf-8")
            )
            entry_size = len(entry.message.encode("utf-8")) + detail_size
            pending = session.execute(
                select(TaskLogSpoolORM)
                .where(TaskLogSpoolORM.published.is_(False))
                .order_by(TaskLogSpoolORM.created_at, TaskLogSpoolORM.id)
            ).scalars().all()
            current_size = sum(
                len(row.message.encode("utf-8"))
                + len(json.dumps(row.detail or {}, ensure_ascii=False).encode("utf-8"))
                for row in pending
            )
            for row in pending:
                if current_size + entry_size <= self._max_spool_bytes:
                    break
                if row.level.lower() == "error":
                    continue
                current_size -= (
                    len(row.message.encode("utf-8"))
                    + len(json.dumps(row.detail or {}, ensure_ascii=False).encode("utf-8"))
                )
                session.execute(
                    delete(TaskLogSpoolORM).where(TaskLogSpoolORM.id == row.id)
                )
            if current_size + entry_size > self._max_spool_bytes:
                if entry.level.lower() != "error":
                    logger.warning(
                        "任务日志 spool 已满，丢弃低等级日志: run=%s sequence=%s",
                        entry.run_id,
                        entry.sequence,
                    )
                    return
                # error 不因容量策略丢弃，即使这会暂时超过上限。
                logger.warning(
                    "任务日志 spool 超限但保留 error: run=%s sequence=%s",
                    entry.run_id,
                    entry.sequence,
                )
            session.execute(
                sqlite_insert(TaskLogSpoolORM)
                .values(
                    run_id=entry.run_id,
                    sequence=entry.sequence,
                    level=entry.level,
                    message=entry.message,
                    detail=entry.detail,
                    published=entry.published,
                )
                .on_conflict_do_nothing(index_elements=["run_id", "sequence"])
            )

    def list_pending_task_logs(self, limit: int) -> list[TaskLogSpoolEntry]:
        """取未上报的任务日志（按 run_id、sequence 排序）。"""
        with self._session_factory.begin() as session:
            rows = session.execute(
                select(TaskLogSpoolORM)
                .where(TaskLogSpoolORM.published.is_(False))
                .order_by(TaskLogSpoolORM.run_id, TaskLogSpoolORM.sequence)
                .limit(limit)
            ).scalars().all()
        return [_to_log(row) for row in rows]

    def mark_task_logs_published(self, ids: list[int]) -> None:
        """标记指定日志已上报。"""
        if not ids:
            return
        with self._session_factory.begin() as session:
            session.execute(
                update(TaskLogSpoolORM)
                .where(TaskLogSpoolORM.id.in_(ids))
                .values(published=True)
            )

    # ---- 脚本缓存 ----

    def cache_script(self, entry: ScriptCacheEntry) -> bool:
        """写入脚本缓存引用；重复（同 hash）返回 False。"""
        with self._session_factory.begin() as session:
            result = session.execute(
                sqlite_insert(ScriptCacheORM)
                .values(
                    script_id=entry.script_id,
                    version=entry.version,
                    sha256=entry.sha256,
                    path=entry.path,
                )
                .on_conflict_do_nothing(
                    index_elements=["script_id", "version", "sha256"]
                )
            )
            return result.rowcount == 1

    def get_cached_script(
        self, script_id: str, version: int, sha256: str
    ) -> ScriptCacheEntry | None:
        """按 (script_id, version, sha256) 查缓存。"""
        with self._session_factory.begin() as session:
            row = session.execute(
                select(ScriptCacheORM).where(
                    ScriptCacheORM.script_id == script_id,
                    ScriptCacheORM.version == version,
                    ScriptCacheORM.sha256 == sha256,
                )
            ).scalar_one_or_none()
        return _to_cache(row) if row is not None else None


def _to_run(row: AgentRunORM) -> AgentRun:
    """ORM 行 → AgentRun 领域对象。"""
    return AgentRun(
        run_id=row.run_id,
        attempt_no=row.attempt_no,
        status=AgentRunStatus(row.status),
        cancelled=row.cancelled,
        result_summary=dict(row.result_summary or {}),
        claimed_at=row.claimed_at,
        updated_at=row.updated_at,
    )


def _to_outbox(row: AgentOutboxORM) -> AgentOutboxEntry:
    """ORM 行 → AgentOutboxEntry 领域对象。"""
    return AgentOutboxEntry(
        outbox_id=row.outbox_id,
        topic=row.topic,
        payload=dict(row.payload or {}),
        status=AgentOutboxStatus(row.status),
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        claimed_until=row.claimed_until,
    )


def _to_log(row: TaskLogSpoolORM) -> TaskLogSpoolEntry:
    """ORM 行 → TaskLogSpoolEntry 领域对象。"""
    return TaskLogSpoolEntry(
        run_id=row.run_id,
        sequence=row.sequence,
        level=row.level,
        message=row.message,
        detail=dict(row.detail or {}),
        published=row.published,
        id=row.id,
    )


def _to_cache(row: ScriptCacheORM) -> ScriptCacheEntry:
    """ORM 行 → ScriptCacheEntry 领域对象。"""
    return ScriptCacheEntry(
        script_id=row.script_id,
        version=row.version,
        sha256=row.sha256,
        path=row.path,
    )
