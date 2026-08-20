"""新增刷新令牌会话表（P2.10 长会话安全）。

设计要点：
- 仅存 SHA-256 哈希（token_hash 唯一），不落库原始令牌；
- user_pk 外键随用户删除级联清理；
- revoked_at 支持登出/改密/禁用账户时撤销；
- replaced_by_hash 构成轮换链，供将来检测旧令牌重放。

Revision ID: 0002_refresh_tokens
Revises: 0001_initial_schema
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from master.adapters.sqlalchemy.orm.base import UTCDateTime

revision: str = "0002_refresh_tokens"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_pk", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("revoked_at", UTCDateTime(), nullable=True),
        sa.Column("replaced_by_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_pk"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_refresh_tokens_user_pk", "refresh_tokens", ["user_pk"]
    )
    op.create_index(
        "ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_expires_at", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_user_pk", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
