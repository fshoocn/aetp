"""持久化 Reporter/Analyzer 扩展结果。

Revision ID: 0041_run_extension_results
Revises: 0040_agent_maintenance_operations
"""

import sqlalchemy as sa
from alembic import op

revision = "0041_run_extension_results"
down_revision = "0040_agent_maintenance_operations"


def upgrade() -> None:
    op.create_table(
        "run_extension_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("extension_id", sa.String(length=64), nullable=False),
        sa.Column("run_pk", sa.Integer(), nullable=False),
        sa.Column("extension_point", sa.String(length=16), nullable=False),
        sa.Column("plugin_id", sa.String(length=255), nullable=False),
        sa.Column("plugin_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="succeeded"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("derived_artifact_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("error_message", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_pk"], ["task_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("extension_id"),
        sa.UniqueConstraint(
            "run_pk",
            "extension_point",
            "plugin_id",
            "plugin_version",
            name="uq_run_extension_results_plugin",
        ),
    )
    op.create_index(
        "ix_run_extension_results_run_point",
        "run_extension_results",
        ["run_pk", "extension_point"],
    )


def downgrade() -> None:
    op.drop_index("ix_run_extension_results_run_point", table_name="run_extension_results")
    op.drop_table("run_extension_results")
