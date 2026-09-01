"""为 V2 Lease/对账流程扩展 Attempt unknown/lost 状态。

Revision ID: 0031_attempt_unknown_lost
Revises: 0030_v2_execution_plans_leases
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_attempt_unknown_lost"
down_revision = "0030_v2_execution_plans_leases"

_CHECK_NAME = "ck_shard_attempts_status"
_CHECK = (
    "status IN ('created','dispatched','acked','running','unknown',"
    "'succeeded','failed','cancelled','timed_out','lost')"
)
_OLD_CHECK = "status IN ('created','dispatched','acked','running','succeeded','failed','cancelled','timed_out')"


def upgrade() -> None:
    with op.batch_alter_table("shard_attempts") as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
        batch_op.create_check_constraint(_CHECK_NAME, _CHECK)


def downgrade() -> None:
    op.execute("UPDATE shard_attempts SET status='failed' WHERE status IN ('unknown','lost')")
    with op.batch_alter_table("shard_attempts") as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
        batch_op.create_check_constraint(_CHECK_NAME, _OLD_CHECK)