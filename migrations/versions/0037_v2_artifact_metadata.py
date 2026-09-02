"""增加 V2 Artifact 元数据和来源字段。

Revision ID: 0037_v2_artifact_metadata
Revises: 0036_v2_case_status_sequence
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0037_v2_artifact_metadata"
down_revision = "0036_v2_case_status_sequence"


def upgrade() -> None:
    op.add_column("run_artifacts", sa.Column("attempt_id", sa.String(length=64), nullable=True))
    op.add_column("run_artifacts", sa.Column("filename", sa.String(length=255), nullable=False, server_default=""))
    op.add_column(
        "run_artifacts",
        sa.Column("content_type", sa.String(length=255), nullable=False, server_default="application/octet-stream"),
    )
    op.add_column("run_artifacts", sa.Column("derived_from", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("run_artifacts", "derived_from")
    op.drop_column("run_artifacts", "content_type")
    op.drop_column("run_artifacts", "filename")
    op.drop_column("run_artifacts", "attempt_id")
