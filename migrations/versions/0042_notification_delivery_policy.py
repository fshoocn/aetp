"""为通知投递增加持久化去重和聚合字段。

Revision ID: 0042_notification_delivery_policy
Revises: 0041_run_extension_results
"""

import sqlalchemy as sa
from alembic import op

revision = "0042_notification_delivery_policy"
down_revision = "0041_run_extension_results"


def upgrade() -> None:
    with op.batch_alter_table("event_deliveries", recreate="always") as batch:
        batch.drop_constraint("uq_event_deliveries_event_subscription", type_="unique")
        batch.add_column(sa.Column("dedupe_key", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("aggregation_key", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("window_ends_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("item_count", sa.Integer(), nullable=True))
        batch.create_unique_constraint(
            "uq_event_deliveries_event_subscription_dedupe",
            ["event_id", "subscription_id", "dedupe_key"],
        )

    op.execute("UPDATE event_deliveries SET dedupe_key = event_id WHERE dedupe_key IS NULL")
    op.execute("UPDATE event_deliveries SET item_count = 1 WHERE item_count IS NULL")
    with op.batch_alter_table("event_deliveries", recreate="always") as batch:
        batch.alter_column("dedupe_key", existing_type=sa.String(length=255), nullable=False)
        batch.alter_column("item_count", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("event_deliveries", recreate="always") as batch:
        batch.drop_constraint("uq_event_deliveries_event_subscription_dedupe", type_="unique")
        batch.drop_column("dedupe_key")
        batch.drop_column("aggregation_key")
        batch.drop_column("window_ends_at")
        batch.drop_column("item_count")
        batch.create_unique_constraint(
            "uq_event_deliveries_event_subscription",
            ["event_id", "subscription_id"],
        )
