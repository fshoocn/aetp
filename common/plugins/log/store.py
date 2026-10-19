"""系统日志查询库（``log.sqlite``）：WAL + 短连接的追加型存储。

与 ``registry.db`` 同款取舍：每操作一开一关的短连接（Windows 句柄安全、
不阻塞清理）；``logs`` 表只索引 ``(ts)`` / ``(node, ts)``（最少索引，
关键字查询 LIKE 扫描——50 万条量级足够）。保留策略双限（天数 + 条数）
先到先删，ingest 后顺带清理。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

#: ingest 时间戳钳制窗：超出 ``[now - 保留天数, now + 1 天]`` 钳到边界并计数
FUTURE_SKEW_S: float = 24 * 3600.0


def _open_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.DatabaseError:
        conn.close()
        raise
    return conn


class LogStore:
    """``log.sqlite``：批量写入 / 游标查询 / 双限保留。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _ensure(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS logs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts REAL NOT NULL, node TEXT NOT NULL, name TEXT NOT NULL, "
            "level INTEGER NOT NULL, op TEXT NOT NULL DEFAULT '', msg TEXT NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_node_ts ON logs(node, ts)")
        conn.commit()

    def write_batch(
        self,
        node: str,
        entries: Sequence[Mapping[str, object]],
        *,
        clamped: list[int] | None = None,
    ) -> int:
        """批量入库（单事务）；``ts`` 越界钳到保留窗边界，越界数记入 ``clamped``。"""
        now = time.time()
        rows: list[tuple[object, ...]] = []
        for entry in entries:
            raw_ts = entry.get("ts")
            ts = float(raw_ts) if isinstance(raw_ts, (int, float)) else now
            bounded = min(max(ts, now - 30 * 86400.0), now + FUTURE_SKEW_S)
            if clamped is not None and bounded != ts:
                clamped.append(1)
            raw_level = entry.get("level")
            rows.append(
                (
                    bounded,
                    node,
                    str(entry.get("name") or "")[:120],
                    int(raw_level) if isinstance(raw_level, (int, float)) else 0,
                    str(entry.get("op") or "")[:64],
                    str(entry.get("msg") or "")[:8192],
                )
            )
        if not rows:
            return 0
        with closing(_open_store(self.path)) as conn, conn:
            self._ensure(conn)
            conn.executemany(
                "INSERT INTO logs (ts, node, name, level, op, msg) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def query(
        self,
        *,
        node: str | None = None,
        level: int | None = None,
        q: str | None = None,
        after_id: int | None = None,
        before_id: int | None = None,
        from_ts: float | None = None,
        to_ts: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        """游标查询（新 → 旧）：``after_id`` / ``before_id`` 二选一（尾随 / 回翻）；
        ``from_ts`` / ``to_ts`` 时间窗（秒）。"""
        # 全字面量 SQL + 可空参数（? IS NULL 即不过滤）——不拼接查询串（S608 合规）
        like: str | None = None
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
        params: tuple[object, ...] = (
            node,
            node,
            level,
            level,
            like,
            like,
            after_id,
            after_id,
            before_id,
            before_id,
            from_ts,
            from_ts,
            to_ts,
            to_ts,
            max(1, min(limit, 1000)),
        )
        if after_id is not None:  # 尾随（after_id）：旧→新追加序
            sql = (
                "SELECT id, ts, node, name, level, op, msg FROM logs "
                "WHERE (? IS NULL OR node = ?) AND (? IS NULL OR level <= ?) "
                "AND (? IS NULL OR msg LIKE ? ESCAPE '\\') "
                "AND (? IS NULL OR id > ?) AND (? IS NULL OR id < ?) "
                "AND (? IS NULL OR ts >= ?) AND (? IS NULL OR ts <= ?) "
                "ORDER BY id ASC LIMIT ?"
            )
        else:  # 回翻 / 首屏：新→旧浏览序
            sql = (
                "SELECT id, ts, node, name, level, op, msg FROM logs "
                "WHERE (? IS NULL OR node = ?) AND (? IS NULL OR level <= ?) "
                "AND (? IS NULL OR msg LIKE ? ESCAPE '\\') "
                "AND (? IS NULL OR id > ?) AND (? IS NULL OR id < ?) "
                "AND (? IS NULL OR ts >= ?) AND (? IS NULL OR ts <= ?) "
                "ORDER BY id DESC LIMIT ?"
            )
        with closing(_open_store(self.path)) as conn:
            self._ensure(conn)
            rows = conn.execute(sql, params).fetchall()
        result = [
            {
                "id": row[0],
                "ts": row[1],
                "node": row[2],
                "name": row[3],
                "level": row[4],
                "op": row[5],
                "msg": row[6],
            }
            for row in rows
        ]
        # after_id（尾随）→ 旧→新追加序；否则（回翻）→ 新→旧浏览序
        return result

    def nodes(self) -> list[str]:
        """上报过日志的节点清单（供过滤下拉）。"""
        with closing(_open_store(self.path)) as conn:
            self._ensure(conn)
            rows = conn.execute("SELECT DISTINCT node FROM logs ORDER BY node").fetchall()
        return [str(row[0]) for row in rows]

    def trim(self, *, retain_days: float, max_rows: int) -> None:
        """双限保留（先到先删）：过期天数 + 总条数上限。"""
        with closing(_open_store(self.path)) as conn, conn:
            self._ensure(conn)
            conn.execute(
                "DELETE FROM logs WHERE ts < ?", (time.time() - retain_days * 86400.0,)
            )
            conn.execute(
                "DELETE FROM logs WHERE id NOT IN "
                "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
                (max(1, max_rows),),
            )


__all__: list[str] = ["FUTURE_SKEW_S", "LogStore"]
