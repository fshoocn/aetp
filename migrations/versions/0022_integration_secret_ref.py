"""project_integrations 增 secret_ref 列（原始 secret 加密存 secret_values）。

Revision ID: 0022
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"


def upgrade() -> None:
    op.add_column(
        "project_integrations",
        sa.Column("secret_ref", sa.String(128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_integrations", "secret_ref")
