"""V2 插件版本、节点期望版本和同步操作 ORM。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, JSONType, TimestampMixin, UTCDateTime


class PluginVersion(Base, TimestampMixin):
    __tablename__ = "plugin_versions"
    __table_args__ = (
        UniqueConstraint("plugin_id", "version", name="uq_plugin_versions_plugin_id"),
        Index("ix_plugin_versions_point_status", "point", "status"),
        CheckConstraint(
            "status IN ('uploaded','verified','installed','pending_restart','enabled','disabled','removed','error')",
            name="ck_plugin_versions_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_id: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    point: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="uploaded")
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    archive_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    installed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class AgentPluginDesiredVersion(Base, TimestampMixin):
    __tablename__ = "agent_plugin_desired_versions"
    __table_args__ = (
        UniqueConstraint("node_id", "plugin_id", name="uq_agent_plugin_desired_node_plugin"),
        Index("ix_agent_plugin_desired_node_id", "node_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plugin_id: Mapped[str] = mapped_column(String(255), nullable=False)
    point: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    auto_update: Mapped[bool] = mapped_column(nullable=False, default=True)
    maintenance_window: Mapped[str | None] = mapped_column(String(128), nullable=True)


class AgentPluginSyncOperation(Base, TimestampMixin):
    __tablename__ = "agent_plugin_sync_operations"
    __table_args__ = (
        UniqueConstraint("sync_id", name="uq_agent_plugin_sync_operations_sync_id"),
        Index("ix_agent_plugin_sync_operations_node_state", "node_id", "state"),
        CheckConstraint(
            "state IN ('pending','draining','installing','restarting','succeeded','failed','cancelled')",
            name="ck_agent_plugin_sync_operations_state",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sync_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    items: Mapped[list[dict[str, object]]] = mapped_column(JSONType, nullable=False)
    results: Mapped[list[dict[str, object]] | None] = mapped_column(JSONType, nullable=True)
    accepted: Mapped[bool | None] = mapped_column(nullable=True)
    restart_required: Mapped[bool] = mapped_column(nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
