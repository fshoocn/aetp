"""Run 执行日志表（P6.4，§6.2/§9.4）。

run_logs：(run_pk, sequence) 唯一，接收端幂等去重；与旧 task_logs
（task 级，关联 tasks）解耦。level CHECK 约束限定 debug/info/warn/error。

Revision ID: 0012_run_logs
Revises: 0011_node_plugin_capabilities
Create Date: 2026-08-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from master.adapters.sqlalchemy.orm.base import JSONType, UTCDateTime

revision: str = "0012_run_logs"
down_revision: Union[str, None] = "0011_node_plugin_capabilities"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "run_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_pk", sa.Integer(), nullable=False),
        sa.Column("shard_pk", sa.Integer(), nullable=True),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", JSONType, nullable=True),
        sa.Column("occurred_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "level IN ('debug','info','warn','error')",
            name="ck_run_logs_level",
        ),
        sa.ForeignKeyConstraint(
            ["run_pk"], ["task_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["shard_pk"], ["run_shards.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_pk", "sequence", name="uq_run_logs_run_sequence"),
    )
    op.create_index("ix_run_logs_run_pk", "run_logs", ["run_pk"])
    op.create_index(
        "ix_run_logs_run_sequence", "run_logs", ["run_pk", "sequence"]
    )


def downgrade() -> None:
    op.drop_index("ix_run_logs_run_sequence", table_name="run_logs")
    op.drop_index("ix_run_logs_run_pk", table_name="run_logs")
    op.drop_table("run_logs")
