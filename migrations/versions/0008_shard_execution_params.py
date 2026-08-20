"""run_shards 增加 execution_params（P3.8 增强）。

Shard 是插件要执行的最小派发单元（子任务）：split_shards 产出每 Shard
专属执行参数（execution_params，如 CAN 通道、测试参数），run.assign 下发，
Agent 插件 execute 时与共享 config 合并/覆盖使用。

Revision ID: 0008_shard_execution_params
Revises: 0007_messaging
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from master.adapters.sqlalchemy.orm.base import JSONType

revision: str = "0008_shard_execution_params"
down_revision: str | None = "0007_messaging"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_shards",
        sa.Column(
            "execution_params",
            JSONType,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("run_shards", "execution_params")
