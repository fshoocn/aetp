"""事件存储端口（P3.7）。

业务层（application services / dispatcher）通过本端口持久化与读取领域事件，
不依赖任何 adapter（SQLAlchemy 等）。现有 DomainEventRepository（P3.5）可作为
本端口的 adapter 实现。

协议基于 §10.4/§6.2：domain_events 表、sequence 全局单调保证事件顺序。
"""

from __future__ import annotations

from typing import Protocol

from master.domain.models import DomainEvent


class EventStore(Protocol):
    """领域事件存储端口。

    事件不可变、按 sequence 单调排序；read 支持项目范围与增量（after_sequence），
    供 SSE 推送 / Hook / 通知按序消费（§5.1）。
    """

    def append(self, event: DomainEvent) -> DomainEvent:
        """持久化一个不可变领域事件（分配 sequence 后回填）。"""
        ...

    def read(
        self,
        *,
        project_id: str | None = None,
        after_sequence: int | None = None,
        limit: int = 100,
    ) -> list[DomainEvent]:
        """按 sequence 升序读取事件；after_sequence 增量拉取。"""
        ...

    def get_by_event_id(self, event_id: str) -> DomainEvent | None:
        """按业务标识取单个事件。"""
        ...
