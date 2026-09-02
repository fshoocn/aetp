"""创建 V2 ScriptDefinition、TestTask revision 和脚本绑定表。

Revision ID: 0032_v2_task_definitions
Revises: 0031_attempt_unknown_lost
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0032_v2_task_definitions"
down_revision = "0031_attempt_unknown_lost"


def upgrade() -> None:
    op.create_table(
        "script_definitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("script_definition_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("executor_plugin_id", sa.String(length=255), nullable=False),
        sa.Column("executor_version", sa.String(length=64), nullable=False),
        sa.Column("executor_archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("cases", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_script_definitions_revision"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "script_definition_id",
            "revision",
            name="uq_script_definitions_definition_revision",
        ),
    )
    op.create_index(
        "ix_script_definitions_project_enabled",
        "script_definitions",
        ["project_id", "enabled"],
    )

    op.create_table(
        "v2_test_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("stop_on_failure", sa.Boolean(), nullable=False),
        sa.Column("retry_policy", sa.JSON(), nullable=False),
        sa.Column("node_ids", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_v2_test_tasks_revision"),
        sa.CheckConstraint(
            "execution_mode IN ('parallel','sequence')",
            name="ck_v2_test_tasks_execution_mode",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "revision", name="uq_v2_test_tasks_task_revision"),
    )
    op.create_index(
        "ix_v2_test_tasks_project_enabled",
        "v2_test_tasks",
        ["project_id", "enabled"],
    )

    op.create_table(
        "v2_test_task_scripts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_pk", sa.Integer(), nullable=False),
        sa.Column("task_revision", sa.Integer(), nullable=False),
        sa.Column("binding_id", sa.String(length=64), nullable=False),
        sa.Column("script_definition_id", sa.String(length=64), nullable=False),
        sa.Column("script_revision", sa.Integer(), nullable=False),
        sa.Column("case_selection", sa.JSON(), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("split_policy", sa.JSON(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("script_revision >= 1", name="ck_v2_test_task_scripts_revision"),
        sa.CheckConstraint("order_index >= 0", name="ck_v2_test_task_scripts_order"),
        sa.ForeignKeyConstraint(["task_pk"], ["v2_test_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_pk",
            "task_revision",
            "binding_id",
            name="uq_v2_test_task_scripts_binding",
        ),
        sa.UniqueConstraint(
            "task_pk",
            "task_revision",
            "order_index",
            name="uq_v2_test_task_scripts_order",
        ),
    )
    op.create_index(
        "ix_v2_test_task_scripts_definition",
        "v2_test_task_scripts",
        ["script_definition_id", "script_revision"],
    )


def downgrade() -> None:
    op.drop_index("ix_v2_test_task_scripts_definition", table_name="v2_test_task_scripts")
    op.drop_table("v2_test_task_scripts")
    op.drop_index("ix_v2_test_tasks_project_enabled", table_name="v2_test_tasks")
    op.drop_table("v2_test_tasks")
    op.drop_index("ix_script_definitions_project_enabled", table_name="script_definitions")
    op.drop_table("script_definitions")
