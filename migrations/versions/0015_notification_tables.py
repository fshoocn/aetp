"""通知三表：notification_endpoints / event_subscriptions / event_deliveries。

Revision ID: 0015
"""

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0013_run_log_fence"


def upgrade() -> None:
    op.create_table(
        "notification_endpoints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("endpoint_id", sa.String(64), unique=True, nullable=False),
        sa.Column(
            "project_pk",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("channel_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("secret_ref", sa.String(128), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("project_pk", "name", name="uq_notification_endpoints_project_name"),
    )
    op.create_index(
        "ix_notification_endpoints_project_enabled",
        "notification_endpoints",
        ["project_pk", "enabled"],
    )

    op.create_table(
        "event_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subscription_id", sa.String(64), unique=True, nullable=False),
        sa.Column(
            "project_pk",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "endpoint_pk",
            sa.Integer(),
            sa.ForeignKey("notification_endpoints.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("event_types", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("filter_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("throttle_policy", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_event_subscriptions_project_enabled",
        "event_subscriptions",
        ["project_pk", "enabled"],
    )

    op.create_table(
        "event_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("delivery_id", sa.String(64), unique=True, nullable=False),
        sa.Column(
            "project_pk",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("event_id", sa.String(64), nullable=False, index=True),
        sa.Column("subscription_id", sa.String(64), nullable=False),
        sa.Column("endpoint_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("response_summary", sa.String(512), nullable=True),
        sa.Column("error_message", sa.String(1024), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "event_id",
            "subscription_id",
            name="uq_event_deliveries_event_subscription",
        ),
    )
    op.create_index(
        "ix_event_deliveries_project_status",
        "event_deliveries",
        ["project_pk", "status"],
    )
    op.create_index(
        "ix_event_deliveries_status_next",
        "event_deliveries",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_table("event_deliveries")
    op.drop_table("event_subscriptions")
    op.drop_table("notification_endpoints")
