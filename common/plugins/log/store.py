"""日志查询库（``aetp.db`` 的 ``aetp_log_entries`` 表）：批量写入 / 游标查询 / 双限保留。

单库设计：日志查询数据住进 ``aetp.db``（Model 归属 ``log``），不再单开
``log.sqlite``；本地留档由文本文件 ``aetp.log`` 负责。查询语义不变：游标
（id）+ 节点 / 级别 / 关键字 / 时间窗过滤，保留策略双限（天数 + 条数）
先到先删，ingest 后顺带清理。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import insert as sa_insert
from sqlalchemy import or_, select
from sqlalchemy.sql.elements import ColumnElement

from common.models import Mapped, Model, mapped_column

#: ingest 时间戳钳制窗：超出 ``[now - 30 天, now + 1 天]`` 钳到边界并计数
FUTURE_SKEW_S: float = 24 * 3600.0

#: 行 → 字典的列序（query / 锚点行返回形状与历史 API 一致）
_COLUMNS: tuple[str, ...] = ("id", "ts", "node", "name", "level", "op", "msg", "source")


class LogEntry(Model):
    """日志查询行（物理表 ``aetp_log_entries``）。"""

    __owner__ = "log"
    __tablename__ = "entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(index=True)
    node: Mapped[str]
    name: Mapped[str]
    level: Mapped[int]
    op: Mapped[str]
    msg: Mapped[str]
    source: Mapped[str]


def _row_dict(row: Sequence[object]) -> dict[str, object]:
    return {name: row[index] for index, name in enumerate(_COLUMNS)}


class LogStore:
    """查询库门面：SQL 经 ``ctx.db`` 执行（单库），对外 API 语义不变。"""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def write_batch(
        self,
        node: str,
        entries: Sequence[Mapping[str, object]],
        *,
        clamped: list[int] | None = None,
    ) -> int:
        """批量入库（单事务）；``ts`` 越界钳到保留窗边界，越界数记入 ``clamped``。"""
        now = time.time()
        rows: list[dict[str, object]] = []
        for entry in entries:
            raw_ts = entry.get("ts")
            ts = float(raw_ts) if isinstance(raw_ts, (int, float)) else now
            bounded = min(max(ts, now - 30 * 86400.0), now + FUTURE_SKEW_S)
            if clamped is not None and bounded != ts:
                clamped.append(1)
            raw_level = entry.get("level")
            rows.append(
                {
                    "ts": bounded,
                    "node": node,
                    "name": str(entry.get("name") or "")[:120],
                    "level": int(raw_level) if isinstance(raw_level, (int, float)) else 0,
                    "op": str(entry.get("op") or "")[:64],
                    "msg": str(entry.get("msg") or "")[:8192],
                    "source": str(entry.get("source") or "")[:240],
                }
            )
        if not rows:
            return 0
        async with self._db.session() as session:
            await session.execute(sa_insert(LogEntry), rows)
        return len(rows)

    def _filters(
        self,
        *,
        node: str | None,
        level: int | None,
        q: str | None,
        after_id: int | None,
        before_id: int | None,
        from_ts: float | None,
        to_ts: float | None,
    ) -> list[ColumnElement[bool]]:
        """过滤谓词链（可空参数即不过滤；关键字命中 msg / op / name / source）。"""
        conditions: list[ColumnElement[bool]] = []
        if node is not None:
            conditions.append(LogEntry.node == node)
        if level is not None:
            conditions.append(LogEntry.level <= level)
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            conditions.append(
                or_(
                    LogEntry.msg.like(like, escape="\\"),
                    LogEntry.op.like(like, escape="\\"),
                    LogEntry.name.like(like, escape="\\"),
                    LogEntry.source.like(like, escape="\\"),
                )
            )
        if after_id is not None:
            conditions.append(LogEntry.id > after_id)
        if before_id is not None:
            conditions.append(LogEntry.id < before_id)
        if from_ts is not None:
            conditions.append(LogEntry.ts >= from_ts)
        if to_ts is not None:
            conditions.append(LogEntry.ts <= to_ts)
        return conditions

    async def query(
        self,
        *,
        node: str | None = None,
        level: int | None = None,
        q: str | None = None,
        after_id: int | None = None,
        before_id: int | None = None,
        around_id: int | None = None,
        from_ts: float | None = None,
        to_ts: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        """游标查询（新 → 旧）：``after_id`` / ``before_id`` 二选一（尾随 / 回翻）；
        ``around_id`` 为**前后文模式**：返回锚点行及其前 / 后各约一半 ``limit``
        条（体验 B，统一时间轴）；``from_ts`` / ``to_ts`` 时间窗（秒）。
        关键字 ``q`` 命中 msg / op / 来源名 / 调用点任一列
        （``q=op-xxxx`` 即「查看本次操作」）。"""
        if around_id is not None:
            half = max(1, min(limit, 1000)) // 2
            older = await self.query(
                node=node, level=level, q=q, before_id=around_id,
                from_ts=from_ts, to_ts=to_ts, limit=half,
            )  # 新→旧
            newer = await self.query(
                node=node, level=level, q=q, after_id=around_id,
                from_ts=from_ts, to_ts=to_ts, limit=half,
            )  # 旧→新
            anchor = await self._row_by_id(around_id)
            combined: list[dict[str, object]] = list(reversed(newer))
            if anchor is not None:
                combined.append(anchor)
            combined.extend(older)
            return combined
        statement = select(
            LogEntry.id, LogEntry.ts, LogEntry.node, LogEntry.name,
            LogEntry.level, LogEntry.op, LogEntry.msg, LogEntry.source,
        ).where(
            *self._filters(
                node=node, level=level, q=q,
                after_id=after_id, before_id=before_id,
                from_ts=from_ts, to_ts=to_ts,
            )
        )
        if after_id is not None:  # 尾随（after_id）：旧→新追加序
            statement = statement.order_by(LogEntry.id.asc())
        else:  # 回翻 / 首屏：新→旧浏览序
            statement = statement.order_by(LogEntry.id.desc())
        statement = statement.limit(max(1, min(limit, 1000)))
        async with self._db.session() as session:
            rows = (await session.execute(statement)).all()
        return [_row_dict(row) for row in rows]

    async def _row_by_id(self, row_id: int) -> dict[str, object] | None:
        """按 id 取单行（前后文锚点；不套过滤，锚点行始终可见）。"""
        statement = select(
            LogEntry.id, LogEntry.ts, LogEntry.node, LogEntry.name,
            LogEntry.level, LogEntry.op, LogEntry.msg, LogEntry.source,
        ).where(LogEntry.id == row_id)
        async with self._db.session() as session:
            row = (await session.execute(statement)).first()
        return _row_dict(row) if row is not None else None

    async def nodes(self) -> list[str]:
        """上报过日志的节点清单（供过滤下拉）。"""
        statement = select(LogEntry.node).distinct().order_by(LogEntry.node)
        async with self._db.session() as session:
            rows = (await session.execute(statement)).all()
        return [str(row[0]) for row in rows]

    async def trim(self, *, retain_days: float, max_rows: int) -> None:
        """双限保留（先到先删）：过期天数 + 总条数上限。"""
        async with self._db.session() as session:
            await session.execute(
                sa_delete(LogEntry).where(LogEntry.ts < time.time() - retain_days * 86400.0)
            )
            keep = select(LogEntry.id).order_by(LogEntry.id.desc()).limit(max(1, max_rows))
            await session.execute(sa_delete(LogEntry).where(LogEntry.id.not_in(keep)))

    async def delete(
        self,
        *,
        node: str | None = None,
        level: int | None = None,
        q: str | None = None,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> int:
        """按筛选条件删除（返回删除行数）；不带条件 = 清空全部。
        关键字谓词与 :meth:`query` 一致（msg / op / 来源名 / 调用点），
        保证「按当前筛选删除」删的就是筛选出的那批行。"""
        conditions = self._filters(
            node=node, level=level, q=q,
            after_id=None, before_id=None, from_ts=from_ts, to_ts=to_ts,
        )
        async with self._db.session() as session:
            result = await session.execute(sa_delete(LogEntry).where(*conditions))
            return int(result.rowcount or 0)


__all__: list[str] = ["FUTURE_SKEW_S", "LogEntry", "LogStore"]
