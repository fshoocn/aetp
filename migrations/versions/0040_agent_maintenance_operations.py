"""创建 Agent 远程运维操作和节点维护锁表。

Revision ID: 0040_agent_maintenance_operations
Revises: 0039_agent_log_events
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0040_agent_maintenance_operations"
down_revision = "0039_agent_log_events"


def upgrade() -> None:
    op.create_table(
        "agent_maintenance_operations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expected_session_id", sa.String(length=128), nullable=True),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("message", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('diagnostics','plugin_sync','log_level','drain','restart')",
            name="ck_agent_maintenance_operations_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','cancelled')",
            name="ck_agent_maintenance_operations_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", name="uq_agent_maintenance_operations_operation_id"),
    )
    op.create_index(
        "ix_agent_maintenance_operations_node_created",
        "agent_maintenance_operations",
        ["node_id", "created_at"],
    )

    op.create_table(
        "node_maintenance_locks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", name="uq_node_maintenance_locks_node_id"),
    )
    op.create_index(
        "ix_node_maintenance_locks_operation",
        "node_maintenance_locks",
        ["operation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_node_maintenance_locks_operation", table_name="node_maintenance_locks")
    op.drop_table("node_maintenance_locks")
    op.drop_index("ix_agent_maintenance_operations_node_created", table_name="agent_maintenance_operations")
    op.drop_table("agent_maintenance_operations")
