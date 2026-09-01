"""创建 V2 插件版本治理表。

Revision ID: 0028_v2_plugin_governance
Revises: 0027_drop_legacy_tasks
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_v2_plugin_governance"
down_revision = "0027_drop_legacy_tasks"


def upgrade() -> None:
    op.create_table(
        "plugin_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plugin_id", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("point", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("archive_path", sa.String(length=1024), nullable=False),
        sa.Column("installed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('uploaded','verified','installed','pending_restart','enabled','disabled','removed','error')",
            name="ck_plugin_versions_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plugin_id", "version", name="uq_plugin_versions_plugin_id"),
    )
    op.create_index(
        "ix_plugin_versions_point_status",
        "plugin_versions",
        ["point", "status"],
    )

    op.create_table(
        "agent_plugin_desired_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("plugin_id", sa.String(length=255), nullable=False),
        sa.Column("point", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("auto_update", sa.Boolean(), nullable=False),
        sa.Column("maintenance_window", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", "plugin_id", name="uq_agent_plugin_desired_node_plugin"),
    )
    op.create_index(
        "ix_agent_plugin_desired_node_id",
        "agent_plugin_desired_versions",
        ["node_id"],
    )

    op.create_table(
        "agent_plugin_sync_operations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sync_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("expected_session_id", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=True),
        sa.Column("restart_required", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending','draining','installing','restarting','succeeded','failed','cancelled')",
            name="ck_agent_plugin_sync_operations_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sync_id", name="uq_agent_plugin_sync_operations_sync_id"),
    )
    op.create_index(
        "ix_agent_plugin_sync_operations_node_state",
        "agent_plugin_sync_operations",
        ["node_id", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_plugin_sync_operations_node_state",
        table_name="agent_plugin_sync_operations",
    )
    op.drop_table("agent_plugin_sync_operations")
    op.drop_index(
        "ix_agent_plugin_desired_node_id",
        table_name="agent_plugin_desired_versions",
    )
    op.drop_table("agent_plugin_desired_versions")
    op.drop_index("ix_plugin_versions_point_status", table_name="plugin_versions")
    op.drop_table("plugin_versions")
