"""Hook 执行审计表（P8.4，§10.6）。

Revision ID: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"


def upgrade() -> None:
    op.create_table(
        "hook_executions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("execution_id", sa.String(64), unique=True, nullable=False),
        sa.Column("event_id", sa.String(64), nullable=True),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("hook_name", sa.String(128), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error_message", sa.String(1024), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_hook_executions_event_hook",
        "hook_executions",
        ["event_id", "hook_name"],
    )


def downgrade() -> None:
    op.drop_table("hook_executions")
