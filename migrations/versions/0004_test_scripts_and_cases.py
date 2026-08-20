"""测试脚本与用例表（P3.2）。

- test_scripts：脚本包元数据，(project_pk, name, version) 唯一，
  sha256 支持同内容幂等复用；parse 字段带 CHECK。
- script_cases：插件解析产出的用例索引，(script_pk, stable_key) 唯一，
  软删除 deleted 标记版本 diff 失效用例。

Revision ID: 0004_test_scripts_and_cases
Revises: 0003_task_status_rename
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from master.adapters.sqlalchemy.orm.base import JSONType, UTCDateTime

revision: str = "0004_test_scripts_and_cases"
down_revision: str | None = "0003_task_status_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "test_scripts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("script_id", sa.String(length=64), nullable=False),
        sa.Column("project_pk", sa.Integer(), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("file_ref", sa.String(length=512), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("config", JSONType, nullable=False),
        sa.Column("hardware_requirements", JSONType, nullable=False),
        sa.Column("parse_status", sa.String(length=16), nullable=False),
        sa.Column("parse_location", sa.String(length=16), nullable=False),
        sa.Column("result_parse_location", sa.String(length=16), nullable=False),
        sa.Column("plugin_version", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("last_parsed_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "parse_status IN ('pending','parsing','parsed','failed')",
            name="ck_test_scripts_parse_status",
        ),
        sa.CheckConstraint(
            "parse_location IN ('master','agent')",
            name="ck_test_scripts_parse_location",
        ),
        sa.CheckConstraint(
            "result_parse_location IN ('master','agent')",
            name="ck_test_scripts_result_parse_location",
        ),
        sa.ForeignKeyConstraint(
            ["project_pk"], ["projects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("script_id"),
        sa.UniqueConstraint(
            "project_pk", "name", "version",
            name="uq_test_scripts_project_name_version",
        ),
    )
    op.create_index("ix_test_scripts_project_pk", "test_scripts", ["project_pk"])
    op.create_index(
        "ix_test_scripts_project_created", "test_scripts", ["project_pk", "created_at"]
    )

    op.create_table(
        "script_cases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("script_pk", sa.Integer(), nullable=False),
        sa.Column("stable_key", sa.String(length=256), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("parent_path", sa.String(length=512), nullable=False),
        sa.Column("tags", JSONType, nullable=False),
        sa.Column("params", JSONType, nullable=False),
        sa.Column("avg_duration_s", sa.Float(), nullable=True),
        sa.Column("duration_samples", sa.Integer(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["script_pk"], ["test_scripts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id"),
        sa.UniqueConstraint(
            "script_pk", "stable_key", name="uq_script_cases_script_stable_key"
        ),
    )
    op.create_index("ix_script_cases_script_pk", "script_cases", ["script_pk"])
    op.create_index(
        "ix_script_cases_script_order", "script_cases", ["script_pk", "order_index"]
    )


def downgrade() -> None:
    op.drop_index("ix_script_cases_script_order", table_name="script_cases")
    op.drop_index("ix_script_cases_script_pk", table_name="script_cases")
    op.drop_table("script_cases")

    op.drop_index("ix_test_scripts_project_created", table_name="test_scripts")
    op.drop_index("ix_test_scripts_project_pk", table_name="test_scripts")
    op.drop_table("test_scripts")
