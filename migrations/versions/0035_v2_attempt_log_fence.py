"""增加 V2 Attempt 日志围栏字段。

Revision ID: 0035_v2_attempt_log_fence
Revises: 0034_v2_attempt_progress_logs
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_v2_attempt_log_fence"
down_revision = "0034_v2_attempt_progress_logs"


def upgrade() -> None:
    op.add_column("shard_attempts", sa.Column("log_complete", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("shard_attempts", sa.Column("last_log_sequence", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("shard_attempts", sa.Column("log_entry_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("shard_attempts", "log_entry_count")
    op.drop_column("shard_attempts", "last_log_sequence")
    op.drop_column("shard_attempts", "log_complete")
