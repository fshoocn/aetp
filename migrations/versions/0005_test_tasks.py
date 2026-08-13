"""测试任务定义表（P3.3）。

- test_tasks：项目内可复用的任务定义，(project_pk, name) 唯一；
  script_pk 引用 test_scripts.id（脚本具体版本），FK RESTRICT 实现引用保护；
  node_ids JSON（⊆ 项目绑定节点，D-23）；定义与执行分离（Run 见 P3.4）。

Revision ID: 0005_test_tasks
Revises: 0004_test_scripts_and_cases
Create Date: 2026-08-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from master.adapters.sqlalchemy.orm.base import JSONType, UTCDateTime

revision: str = "0005_test_tasks"
down_revision: Union[str, None] = "0004_test_scripts_and_cases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "test_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("project_pk", sa.Integer(), nullable=False),
        sa.Column("script_pk", sa.Integer(), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("default_case_selection", JSONType, nullable=False),
        sa.Column("node_ids", JSONType, nullable=False),
        sa.Column("split_policy", JSONType, nullable=False),
        sa.Column("max_parallel_shards", sa.Integer(), nullable=False),
        sa.Column("retry_policy", JSONType, nullable=False),
        sa.Column("timeout_s", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_pk"], ["projects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["script_pk"], ["test_scripts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
        sa.UniqueConstraint(
            "project_pk", "name", name="uq_test_tasks_project_name"
        ),
    )
    op.create_index("ix_test_tasks_project_pk", "test_tasks", ["project_pk"])
    op.create_index("ix_test_tasks_script_pk", "test_tasks", ["script_pk"])
    op.create_index(
        "ix_test_tasks_project_enabled_created",
        "test_tasks",
        ["project_pk", "enabled", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_test_tasks_project_enabled_created", table_name="test_tasks"
    )
    op.drop_index("ix_test_tasks_script_pk", table_name="test_tasks")
    op.drop_index("ix_test_tasks_project_pk", table_name="test_tasks")
    op.drop_table("test_tasks")
