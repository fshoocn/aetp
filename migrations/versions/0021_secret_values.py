"""secret_values 表：加密密钥存储（§12.2/§10.5）。

密钥以 Fernet 密文落库，业务层只持有 secret_ref。

Revision ID: 0021
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"


def upgrade() -> None:
    op.create_table(
        "secret_values",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("secret_ref", sa.String(128), unique=True, nullable=False),
        sa.Column("cipher_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_secret_values_secret_ref",
        "secret_values",
        ["secret_ref"],
    )


def downgrade() -> None:
    op.drop_index("ix_secret_values_secret_ref", table_name="secret_values")
    op.drop_table("secret_values")
