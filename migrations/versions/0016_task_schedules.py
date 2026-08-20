"""任务调度计划表（D-18，§18.7）。

Revision ID: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"


def upgrade() -> None:
    op.create_table(
        "task_schedules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("schedule_id", sa.String(64), unique=True, nullable=False),
        sa.Column(
            "task_pk",
            sa.Integer(),
            sa.ForeignKey("test_tasks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("cron_expression", sa.String(128), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "(cron_expression IS NULL AND interval_seconds IS NOT NULL) OR "
            "(cron_expression IS NOT NULL AND interval_seconds IS NULL)",
            name="ck_task_schedules_cron_or_interval",
        ),
    )
    op.create_index(
        "ix_task_schedules_enabled_next",
        "task_schedules",
        ["enabled", "next_run_at"],
    )


def downgrade() -> None:
    op.drop_table("task_schedules")
