"""事件订阅增加可选测试任务绑定。"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"


def upgrade() -> None:
    with op.batch_alter_table("event_subscriptions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "task_pk",
                sa.Integer(),
                sa.ForeignKey("test_tasks.id", ondelete="CASCADE"),
                nullable=True,
            )
        )
    op.create_index("ix_event_subscriptions_task_pk", "event_subscriptions", ["task_pk"])


def downgrade() -> None:
    op.drop_index("ix_event_subscriptions_task_pk", table_name="event_subscriptions")
    with op.batch_alter_table("event_subscriptions") as batch_op:
        batch_op.drop_column("task_pk")
