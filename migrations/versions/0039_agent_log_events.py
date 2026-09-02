"""创建 Agent 结构化日志索引表。

Revision ID: 0039_agent_log_events
Revises: 0038_v2_script_requirements
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0039_agent_log_events"
down_revision = "0038_v2_script_requirements"


def upgrade() -> None:
    op.create_table(
        "agent_log_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("component", sa.String(length=128), nullable=False),
        sa.Column("event_code", sa.String(length=128), nullable=False),
        sa.Column("message_template", sa.String(length=1024), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("plugin_id", sa.String(length=255), nullable=True),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("exception", sa.JSON(), nullable=True),
        sa.Column("event", sa.JSON(), nullable=False),
        sa.Column("batch_first_sequence", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "node_id",
            "session_id",
            "sequence",
            name="uq_agent_log_node_session_sequence",
        ),
    )
    op.create_index(
        "ix_agent_log_node_occurred",
        "agent_log_events",
        ["node_id", "occurred_at"],
    )
    op.create_index(
        "ix_agent_log_node_level_component",
        "agent_log_events",
        ["node_id", "level", "component"],
    )
    op.create_index(
        "ix_agent_log_run_attempt",
        "agent_log_events",
        ["run_id", "attempt_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_log_run_attempt", table_name="agent_log_events")
    op.drop_index("ix_agent_log_node_level_component", table_name="agent_log_events")
    op.drop_index("ix_agent_log_node_occurred", table_name="agent_log_events")
    op.drop_table("agent_log_events")
