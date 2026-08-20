"""test_tasks.script_pk 改为可空（支持停用任务解除脚本引用）。

Revision ID: 0019
"""

from alembic import op
from sqlalchemy import text

revision = "0019"
down_revision = "0018"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite 不支持 ALTER COLUMN；直接用 batch_alter_table 时外键约束
        # 会导致 DROP TABLE 失败。需要先禁用外键、清理可能残留的临时表。
        op.execute(text("PRAGMA foreign_keys=OFF"))
        op.execute(text("DROP TABLE IF EXISTS _alembic_tmp_test_tasks"))
        with op.batch_alter_table("test_tasks", schema=None) as batch_op:
            batch_op.alter_column(
                "script_pk",
                existing_nullable=False,
                nullable=True,
            )
        op.execute(text("PRAGMA foreign_keys=ON"))
    else:
        op.alter_column(
            "test_tasks",
            "script_pk",
            existing_nullable=False,
            nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(text("PRAGMA foreign_keys=OFF"))
        op.execute(text("DROP TABLE IF EXISTS _alembic_tmp_test_tasks"))
        with op.batch_alter_table("test_tasks", schema=None) as batch_op:
            batch_op.alter_column(
                "script_pk",
                existing_nullable=True,
                nullable=False,
            )
        op.execute(text("PRAGMA foreign_keys=ON"))
    else:
        op.alter_column(
            "test_tasks",
            "script_pk",
            existing_nullable=True,
            nullable=False,
        )
