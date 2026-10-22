"""持久化 outbox（决策 A1 / A3）：经 db 插件落 ``aetp_comm_outbox``，成功即删。

规则（``docs/comm通信方案-2026-10-22.md`` §9.1）：

* **入队即写盘**：``min_success >= 1`` 的消息落盘；``min_success = 0``
  （fire-and-forget）仅内存、不落盘；
* **发送成功即删**：``acks >= min_success`` 达标后立即删除；
* **重启恢复**：未终结行重新入队续传，``attempts`` 保留；ttl 过期直接清理。

db 是所有数据库操作的唯一入口（分层 ``appconfig → db → log / comm``，
db 不依赖 comm），outbox 表随 ``aetp.db`` 统一管理。
"""

from __future__ import annotations

from typing import Any

from common.plugins.db import Mapped, Model, mapped_column

#: 行结构（逻辑列）的固定顺序（``load_all`` / 补丁用）
OUTBOX_COLUMNS: tuple[str, ...] = (
    "id",
    "group_id",
    "scope",
    "kind",
    "dest",
    "topic",
    "payload",
    "ts",
    "ttl",
    "min_success",
    "min_retries",
    "max_attempts",
    "attempts",
    "acks",
    "state",
    "last_error",
    "created_ts",
    "updated_ts",
)


class CommOutbox(Model):
    """至少投递 1 次消息的持久化记录（归属 comm，物理表 ``aetp_comm_outbox``）。"""

    __owner__ = "comm"
    __tablename__ = "outbox"

    id: Mapped[str] = mapped_column(primary_key=True)
    group_id: Mapped[str]
    scope: Mapped[str]
    kind: Mapped[str]
    dest: Mapped[str]
    topic: Mapped[str]
    #: JSON 文本（payload 可为任意 JSON 值，统一文本列避免类型歧义）
    payload: Mapped[str]
    ts: Mapped[float]
    ttl: Mapped[float]
    min_success: Mapped[int]
    min_retries: Mapped[int]
    max_attempts: Mapped[int]
    attempts: Mapped[int]
    acks: Mapped[int]
    state: Mapped[str]
    last_error: Mapped[str]
    created_ts: Mapped[float]
    updated_ts: Mapped[float]


class OutboxStore:
    """outbox 行的读写封装（单操作单事务，经 db 仓储）。"""

    def __init__(self, db: Any) -> None:
        self._repo = db.repo(CommOutbox, owner="comm")

    async def save(self, values: dict[str, Any]) -> None:
        """插入一行（幂等由调用方保证：id 不重复）。"""
        await self._repo.add(CommOutbox(**values))

    async def patch(self, entry_id: str, values: dict[str, Any]) -> None:
        """按 id 更新若干列（不存在则忽略）。"""
        await self._repo.update(entry_id, values)

    async def remove(self, entry_id: str) -> None:
        """按 id 删除一行（不存在则忽略）。"""
        await self._repo.delete(entry_id)

    async def load_all(self) -> list[dict[str, Any]]:
        """读出全部行（启动恢复用），按 ``OUTBOX_COLUMNS`` 返回字典。"""
        rows = await self._repo.list()
        return [{name: getattr(row, name) for name in OUTBOX_COLUMNS} for row in rows]

    async def size(self) -> int:
        """当前行数（自监控指标用）。"""
        return await self._repo.count()


__all__: list[str] = ["OUTBOX_COLUMNS", "CommOutbox", "OutboxStore"]
