"""增加 V2 Attempt 进度和按 Attempt 区分的日志身份。

Revision ID: 0034_v2_attempt_progress_logs
Revises: 0033_v2_run_snapshots
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0034_v2_attempt_progress_logs"
down_revision = "0033_v2_run_snapshots"


def upgrade() -> None:
    op.add_column(
        "shard_attempts",
        sa.Column("last_progress_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    with op.batch_alter_table("run_logs", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("attempt_id", sa.String(length=64), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("plan_id", sa.String(length=64), nullable=True))
        batch_op.drop_constraint("uq_run_logs_run_sequence", type_="unique")
        batch_op.create_unique_constraint(
            "uq_run_logs_run_attempt_sequence",
            ["run_pk", "attempt_id", "sequence"],
        )


def downgrade() -> None:
    with op.batch_alter_table("run_logs", recreate="always") as batch_op:
        batch_op.drop_constraint("uq_run_logs_run_attempt_sequence", type_="unique")
        batch_op.create_unique_constraint("uq_run_logs_run_sequence", ["run_pk", "sequence"])
        batch_op.drop_column("plan_id")
        batch_op.drop_column("attempt_id")
    op.drop_column("shard_attempts", "last_progress_sequence")
