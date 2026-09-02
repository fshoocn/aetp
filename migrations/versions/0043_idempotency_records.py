"""增加写 API 的持久化幂等键记录。

Revision ID: 0043_idempotency_records
Revises: 0042_notification_delivery_policy
"""

import sqlalchemy as sa
from alembic import op

revision = "0043_idempotency_records"
down_revision = "0042_notification_delivery_policy"


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.String(length=512), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
    )
    op.create_index(
        "ix_idempotency_expires_at",
        "idempotency_records",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_expires_at", table_name="idempotency_records")
    op.drop_table("idempotency_records")
