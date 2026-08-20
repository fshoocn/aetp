"""初始基线：创建 AETP Master 全部表结构。

设计要点：
- 统一使用代理主键 int 外键（project_pk / node_pk / device_pk / task_pk），
  业务字符串标识（project_id / node_id / device_id / task_id）仅作唯一业务键；
- JSON 列：task.command/result、node.tags/capabilities（PostgreSQL 为 JSONB）；
- 时间戳统一 UTC（UTCDateTime，写库为 naive UTC）；
- 状态字段带 CheckConstraint，防止魔法字符串进入数据库。

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from master.adapters.sqlalchemy.orm.base import JSONType, UTCDateTime

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("account_status", sa.String(length=16), nullable=False),
        sa.Column("platform_role", sa.String(length=16), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "account_status IN ('pending','active','disabled')",
            name="ck_users_account_status",
        ),
        sa.CheckConstraint(
            "platform_role IN ('user','admin')", name="ck_users_platform_role"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "nodes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("online", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("tags", JSONType, nullable=False),
        sa.Column("capabilities", JSONType, nullable=False),
        sa.Column("protocol_version", sa.String(length=32), nullable=False),
        sa.Column("last_seen_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('offline','online')", name="ck_nodes_status"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id"),
    )
    op.create_index("ix_nodes_status", "nodes", ["status"])
    op.create_index("ix_nodes_online", "nodes", ["online"])
    op.create_table(
        "devices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("node_pk", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("online", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('offline','online','busy')", name="ck_devices_status"
        ),
        sa.ForeignKeyConstraint(
            ["node_pk"], ["nodes.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id"),
    )
    op.create_index("ix_devices_node_pk", "devices", ["node_pk"])
    op.create_index("ix_devices_online", "devices", ["online"])
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("project_key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','archived')", name="ck_projects_status"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id"),
        sa.UniqueConstraint("project_key"),
    )
    op.create_index("ix_projects_status", "projects", ["status"])
    op.create_table(
        "project_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_pk", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_role", sa.String(length=16), nullable=False),
        sa.Column("assigned_by", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "project_role IN ('viewer','operator','maintainer','owner')",
            name="ck_project_members_role",
        ),
        sa.ForeignKeyConstraint(["project_pk"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_pk", "user_id", name="uq_project_members_project_user"
        ),
    )
    op.create_index("ix_project_members_project_pk", "project_members", ["project_pk"])
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])
    op.create_table(
        "project_node_bindings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_pk", sa.Integer(), nullable=False),
        sa.Column("node_pk", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("assigned_by", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_pk"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_pk"], ["nodes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_pk", "node_pk", name="uq_project_node_bindings_project_node"
        ),
    )
    op.create_index(
        "ix_project_node_bindings_project_pk",
        "project_node_bindings",
        ["project_pk"],
    )
    op.create_index(
        "ix_project_node_bindings_node_pk", "project_node_bindings", ["node_pk"]
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("project_pk", sa.Integer(), nullable=False),
        sa.Column("device_pk", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("command", JSONType, nullable=False),
        sa.Column("result", JSONType, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", UTCDateTime(), nullable=True),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','dispatched','accepted','running',"
            "'completed','failed','cancelled','timeout')",
            name="ck_tasks_status",
        ),
        sa.ForeignKeyConstraint(["project_pk"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["device_pk"], ["devices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
    )
    op.create_index("ix_tasks_project_pk", "tasks", ["project_pk"])
    op.create_index("ix_tasks_device_pk", "tasks", ["device_pk"])
    op.create_index("ix_tasks_project_status", "tasks", ["project_pk", "status"])
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])
    op.create_table(
        "task_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_pk", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("ts", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_pk"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_logs_task_pk", "task_logs", ["task_pk"])


def downgrade() -> None:
    op.drop_table("task_logs")
    op.drop_table("tasks")
    op.drop_table("project_node_bindings")
    op.drop_table("project_members")
    op.drop_table("projects")
    op.drop_table("devices")
    op.drop_table("nodes")
    op.drop_table("users")
