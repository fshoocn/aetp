"""为 V2 Run Snapshot 和脚本绑定增加持久化列。

Revision ID: 0033_v2_run_snapshots
Revises: 0032_v2_task_definitions
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0033_v2_run_snapshots"
down_revision = "0032_v2_task_definitions"


def upgrade() -> None:
    op.add_column("task_runs", sa.Column("task_id", sa.String(length=64), nullable=True))
    op.add_column("task_runs", sa.Column("task_revision", sa.Integer(), nullable=True))
    op.add_column("task_runs", sa.Column("task_snapshot", sa.JSON(), nullable=True))
    op.create_index("ix_task_runs_task_id", "task_runs", ["task_id"])
    op.add_column(
        "run_shards",
        sa.Column("script_binding_id", sa.String(length=64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("run_shards", "script_binding_id")
    op.drop_index("ix_task_runs_task_id", table_name="task_runs")
    op.drop_column("task_runs", "task_snapshot")
    op.drop_column("task_runs", "task_revision")
    op.drop_column("task_runs", "task_id")
