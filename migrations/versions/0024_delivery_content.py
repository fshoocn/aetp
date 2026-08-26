"""event_deliveries 增加投递内容快照。"""

import sqlalchemy as sa
from alembic import op

from master.adapters.sqlalchemy.orm.base import JSONType

revision = "0024"
down_revision = "0023"


def upgrade() -> None:
    op.add_column(
        "event_deliveries",
        sa.Column("content", JSONType, nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("event_deliveries", "content")