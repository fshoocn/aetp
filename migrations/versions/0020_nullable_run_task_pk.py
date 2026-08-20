"""task_runs.task_pk 与 results.task_pk 改为可空（任务定义删除后保留历史）。

Revision ID: 0020
"""

from alembic import op
from sqlalchemy import text

revision = "0020"
down_revision = "0019"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(text("PRAGMA foreign_keys=OFF"))
        op.execute(text("DROP TABLE IF EXISTS _alembic_tmp_task_runs"))
        op.execute(text("DROP TABLE IF EXISTS _alembic_tmp_results"))
        with op.batch_alter_table("task_runs", schema=None) as batch_op:
            batch_op.alter_column(
                "task_pk",
                existing_nullable=False,
                nullable=True,
            )
        with op.batch_alter_table("results", schema=None) as batch_op:
            batch_op.alter_column(
                "task_pk",
                existing_nullable=False,
                nullable=True,
            )
        op.execute(text("PRAGMA foreign_keys=ON"))
    else:
        op.alter_column("task_runs", "task_pk", existing_nullable=False, nullable=True)
        op.alter_column("results", "task_pk", existing_nullable=False, nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(text("PRAGMA foreign_keys=OFF"))
        op.execute(text("DROP TABLE IF EXISTS _alembic_tmp_task_runs"))
        op.execute(text("DROP TABLE IF EXISTS _alembic_tmp_results"))
        with op.batch_alter_table("task_runs", schema=None) as batch_op:
            batch_op.alter_column(
                "task_pk",
                existing_nullable=True,
                nullable=False,
            )
        with op.batch_alter_table("results", schema=None) as batch_op:
            batch_op.alter_column(
                "task_pk",
                existing_nullable=True,
                nullable=False,
            )
        op.execute(text("PRAGMA foreign_keys=ON"))
    else:
        op.alter_column("task_runs", "task_pk", existing_nullable=True, nullable=False)
        op.alter_column("results", "task_pk", existing_nullable=True, nullable=False)
