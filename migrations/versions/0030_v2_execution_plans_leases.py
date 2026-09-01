"""创建 V2 ExecutionPlan 和 ResourceLease 表。

Revision ID: 0030_v2_execution_plans_leases
Revises: 0029_v2_capability_diagnostics
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_v2_execution_plans_leases"
down_revision = "0029_v2_capability_diagnostics"


def upgrade() -> None:
    op.create_table(
        "execution_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("script_binding_id", sa.String(length=64), nullable=False),
        sa.Column("script_definition_id", sa.String(length=64), nullable=False),
        sa.Column("shard_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("target_session_id", sa.String(length=128), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("deadline_at", sa.DateTime(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", name="uq_execution_plans_plan_id"),
        sa.UniqueConstraint(
            "run_id",
            "script_binding_id",
            "shard_id",
            "attempt_no",
            name="uq_execution_plans_run_binding_shard_attempt",
        ),
    )
    op.create_index(
        "ix_execution_plans_node_deadline",
        "execution_plans",
        ["node_id", "deadline_at"],
    )
    op.create_index("ix_execution_plans_run", "execution_plans", ["run_id"])

    op.create_table(
        "resource_leases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lease_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("shard_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('active','released','expired')",
            name="ck_resource_leases_state",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_resource_leases_revision"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lease_id", name="uq_resource_leases_lease_id"),
    )
    op.create_index(
        "ix_resource_leases_attempt",
        "resource_leases",
        ["attempt_id", "state"],
    )
    op.create_index(
        "ix_resource_leases_expires",
        "resource_leases",
        ["state", "expires_at"],
    )
    op.create_index(
        "uq_resource_leases_active_resource",
        "resource_leases",
        ["resource_id"],
        unique=True,
        sqlite_where=sa.text("state = 'active'"),
        postgresql_where=sa.text("state = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_resource_leases_active_resource", table_name="resource_leases")
    op.drop_index("ix_resource_leases_expires", table_name="resource_leases")
    op.drop_index("ix_resource_leases_attempt", table_name="resource_leases")
    op.drop_table("resource_leases")
    op.drop_index("ix_execution_plans_run", table_name="execution_plans")
    op.drop_index("ix_execution_plans_node_deadline", table_name="execution_plans")
    op.drop_table("execution_plans")
