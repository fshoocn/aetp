"""ORM：设备。

device_id 为业务标识（MQTT client_id / topic），全局唯一。
node_pk 为所属节点的代理主键外键（int FK，保证参照完整性）。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .node import Node


class Device(Base, TimestampMixin):
    __tablename__ = "devices"
    __table_args__ = (
        Index("ix_devices_online", "online"),
        CheckConstraint("status IN ('offline','online','busy')", name="ck_devices_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    node_pk: Mapped[Optional[int]] = mapped_column(
        ForeignKey("nodes.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="offline"
    )
    online: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )

    node: Mapped[Optional["Node"]] = relationship(
        back_populates="devices",
    )
