"""task_runs 增日志围栏列（P6.6，§8.4 run.log-complete）。

- log_complete：Agent 发布 run.log-complete 后置位，Master 此后拒绝日志；
- last_log_sequence：围栏时记录的末条日志 sequence。

Revision ID: 0013_run_log_fence
Revises: 0012_run_logs
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_run_log_fence"
down_revision: str | None = "0012_run_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_runs",
        sa.Column(
            "log_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "task_runs",
        sa.Column("last_log_sequence", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task_runs", "last_log_sequence")
    op.drop_column("task_runs", "log_complete")
