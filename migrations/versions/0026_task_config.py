"""测试任务定义增加插件执行配置。"""

import sqlalchemy as sa
from alembic import op

from master.adapters.sqlalchemy.orm.base import JSONType

revision = "0026"
down_revision = "0025"


def upgrade() -> None:
    with op.batch_alter_table("test_tasks") as batch_op:
        batch_op.add_column(sa.Column("config", JSONType, nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("test_tasks") as batch_op:
        batch_op.drop_column("config")