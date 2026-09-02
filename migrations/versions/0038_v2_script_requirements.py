"""保存 V2 ScriptDefinition 的执行需求快照。

Revision ID: 0038_v2_script_requirements
Revises: 0037_v2_artifact_metadata
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0038_v2_script_requirements"
down_revision = "0037_v2_artifact_metadata"


def upgrade() -> None:
    op.add_column("script_definitions", sa.Column("requirement", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("script_definitions", "requirement")
