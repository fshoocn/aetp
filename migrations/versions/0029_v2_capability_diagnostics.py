"""创建 V2 节点能力和诊断快照表。

Revision ID: 0029_v2_capability_diagnostics
Revises: 0028_v2_plugin_governance
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_v2_capability_diagnostics"
down_revision = "0028_v2_plugin_governance"


def upgrade() -> None:
    op.create_table(
        "node_capability_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("reported_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_node_capability_snapshots_revision"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "node_id",
            "session_id",
            "revision",
            name="uq_node_capability_snapshots_node_session_revision",
        ),
    )
    op.create_index(
        "ix_node_capability_snapshots_node_created",
        "node_capability_snapshots",
        ["node_id", "created_at"],
    )

    op.create_table(
        "agent_diagnostics_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_agent_diagnostics_snapshots_request_id"),
        sa.UniqueConstraint(
            "node_id",
            "collected_at",
            name="uq_agent_diagnostics_snapshots_node_collected",
        ),
    )
    op.create_index(
        "ix_agent_diagnostics_snapshots_node_collected",
        "agent_diagnostics_snapshots",
        ["node_id", "collected_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_diagnostics_snapshots_node_collected",
        table_name="agent_diagnostics_snapshots",
    )
    op.drop_table("agent_diagnostics_snapshots")
    op.drop_index(
        "ix_node_capability_snapshots_node_created",
        table_name="node_capability_snapshots",
    )
    op.drop_table("node_capability_snapshots")
