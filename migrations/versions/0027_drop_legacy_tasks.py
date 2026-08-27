"""废除旧 ``tasks``/``task_logs`` 双轨（D-D-01，§18 任务全生命周期）。

任务定义已由 ``test_tasks`` 主轨承担，执行实体为 ``task_runs``（Run/Shard/
Attempt），旧 ``tasks``（占位任务）+ ``task_logs``（任务级日志）自 v4.28
起不再被任何服务/路由消费。本迁移删除两张遗留表，消除双轨。

注意：``0003_task_status_rename`` 仅作用于 ``tasks.status``，其历史回填
语义在删除表后不再有意义，但保留该迁移文件以维持迁移链可追溯（开发
阶段不考虑兼容，升级到本 revision 后旧轨彻底移除）。

Revision ID: 0027_drop_legacy_tasks
Revises: 0026
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0027_drop_legacy_tasks"
down_revision = "0026"


def upgrade() -> None:
    # 先删外键指向 tasks 的子表，再删主表（task_logs.task_pk -> tasks.id）
    op.drop_index("ix_task_logs_task_pk", table_name="task_logs")
    op.drop_table("task_logs")
    op.drop_index("ix_tasks_project_pk", table_name="tasks")
    op.drop_index("ix_tasks_device_pk", table_name="tasks")
    op.drop_index("ix_tasks_project_status", table_name="tasks")
    op.drop_index("ix_tasks_created_at", table_name="tasks")
    op.drop_table("tasks")


def downgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("project_pk", sa.Integer(), nullable=False),
        sa.Column("device_pk", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("command", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','dispatching','running','cancelling','succeeded','failed','cancelled','timed_out')",
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
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_pk"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_logs_task_pk", "task_logs", ["task_pk"])
