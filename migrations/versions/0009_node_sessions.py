"""node_sessions 表 + nodes 在线投影扩展（P4.4）。

节点会话（每次进程启动一个新 session_id，隔离旧连接，§8.6）：
- 新建 node_sessions 表（(node_pk, session_id) 唯一；disconnected_at 非空=已关闭）
- nodes 增加 load_json（心跳上报结构化负载 {running_shards, queued_shards}，§18.5）
- nodes.status CHECK 扩展支持 busy/disabled（D-22：P4 节点在线投影新增）

Revision ID: 0009_node_sessions
Revises: 0008_shard_execution_params
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from master.adapters.sqlalchemy.orm.base import JSONType, UTCDateTime

revision: str = "0009_node_sessions"
down_revision: str | None = "0008_shard_execution_params"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# batch 操作会再次应用命名约定：ck_nodes_ck_nodes_status
_DB_CHECK_NAME = "ck_nodes_status"
_NEW_CHECK = "status IN ('offline','online','busy','disabled')"
_OLD_CHECK = "status IN ('offline','online')"


def upgrade() -> None:
    # 1. node_sessions 表
    op.create_table(
        "node_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "node_pk",
            sa.Integer(),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=128), nullable=False),
        sa.Column("connected_at", UTCDateTime(), nullable=False),
        sa.Column("disconnected_at", UTCDateTime(), nullable=True),
        sa.Column("disconnect_reason", sa.String(length=32), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_pk", "session_id", name="uq_node_sessions_node_session"),
    )
    op.create_index(
        "ix_node_sessions_node_current",
        "node_sessions",
        ["node_pk", "disconnected_at"],
    )

    # 2. nodes 增加负载列
    op.add_column(
        "nodes",
        sa.Column("load", JSONType, nullable=False, server_default=sa.text("'{}'")),
    )

    # 3. nodes.status CHECK 扩展为 busy/disabled（现有数据均为 offline/online，直接重建）
    with op.batch_alter_table("nodes") as batch_op:
        batch_op.drop_constraint(_DB_CHECK_NAME, type_="check")
        batch_op.create_check_constraint("ck_nodes_status", _NEW_CHECK)


def downgrade() -> None:
    # 1. 恢复 CHECK（busy/disabled 值降级为 offline/online 前的原约束；有损合并留给调用方）
    with op.batch_alter_table("nodes") as batch_op:
        batch_op.drop_constraint(_DB_CHECK_NAME, type_="check")
        batch_op.create_check_constraint("ck_nodes_status", _OLD_CHECK)

    # 2. 移除负载列
    op.drop_column("nodes", "load")

    # 3. 删除 node_sessions
    op.drop_index("ix_node_sessions_node_current", table_name="node_sessions")
    op.drop_table("node_sessions")
