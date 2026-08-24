"""nodes 增 resource_occupancy 列（资源占用映射 device_id -> run_id，§9.8）。

Revision ID: 0023
"""

import sqlalchemy as sa
from alembic import op

from master.adapters.sqlalchemy.orm.base import JSONType

revision = "0023"
down_revision = "0022"


def upgrade() -> None:
    op.add_column(
        "nodes",
        sa.Column(
            "resource_occupancy",
            JSONType,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("nodes", "resource_occupancy")
