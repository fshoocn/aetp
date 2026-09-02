"""增加 V2 case-status 序号。

Revision ID: 0036_v2_case_status_sequence
Revises: 0035_v2_attempt_log_fence
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0036_v2_case_status_sequence"
down_revision = "0035_v2_attempt_log_fence"


def upgrade() -> None:
    op.add_column("run_case_results", sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("run_case_results", "sequence")
