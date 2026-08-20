"""持久化 Agent 插件版本与兼容版本（P5.5）。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from master.adapters.sqlalchemy.orm.base import JSONType

revision: str = "0011_node_plugin_capabilities"
down_revision: str | None = "0010_device_resource_allocation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "nodes",
        sa.Column(
            "plugin_versions",
            JSONType,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "nodes",
        sa.Column(
            "plugin_supported_versions",
            JSONType,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("nodes", "plugin_supported_versions")
    op.drop_column("nodes", "plugin_versions")
