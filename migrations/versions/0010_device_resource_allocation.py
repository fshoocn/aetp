"""设备资源能力与 Attempt 多资源占用（P4.6）。

设备能力描述保存在 devices.capabilities；一次 Attempt 可以原子占用
多个设备，shard_attempts.device_ids 保存完整资源集合。任务级和节点级
并发容量不再作为调度限制，设备可用性由 Master 排队决定。

Revision ID: 0010_device_resource_allocation
Revises: 0009_node_sessions
Create Date: 2026-08-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from master.adapters.sqlalchemy.orm.base import JSONType

revision: str = "0010_device_resource_allocation"
down_revision: Union[str, None] = "0009_node_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column(
            "capabilities",
            JSONType,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "shard_attempts",
        sa.Column(
            "device_ids",
            JSONType,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    with op.batch_alter_table("test_tasks") as batch_op:
        batch_op.drop_column("max_parallel_shards")
    with op.batch_alter_table("run_shards") as batch_op:
        batch_op.drop_column("mutex_keys")


def downgrade() -> None:
    with op.batch_alter_table("test_tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "max_parallel_shards",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
    with op.batch_alter_table("run_shards") as batch_op:
        batch_op.add_column(
            sa.Column(
                "mutex_keys",
                JSONType,
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
    op.drop_column("shard_attempts", "device_ids")
    op.drop_column("devices", "capabilities")
