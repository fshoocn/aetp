"""ORM：Agent 节点。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .device import Device


class Node(Base, TimestampMixin):
    __tablename__ = "nodes"
    __table_args__ = (
        Index("ix_nodes_status", "status"),
        Index("ix_nodes_online", "online"),
        CheckConstraint("status IN ('offline','online')", name="ck_nodes_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="offline"
    )
    online: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tags: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    capabilities: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    protocol_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default=""
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )

    devices: Mapped[list["Device"]] = relationship(
        back_populates="node",
        passive_deletes=True,
    )
