"""CI/CD 集成表（P8.3，§8.8）。

project_integrations / ci_trigger_bindings / ci_webhook_deliveries。

Revision ID: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"


def upgrade() -> None:
    op.create_table(
        "project_integrations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("integration_id", sa.String(64), unique=True, nullable=False),
        sa.Column(
            "project_pk",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("secret_hash", sa.String(128), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("project_pk", "name", name="uq_project_integrations_project_name"),
    )
    op.create_index(
        "ix_project_integrations_project_enabled",
        "project_integrations",
        ["project_pk", "enabled"],
    )

    op.create_table(
        "ci_trigger_bindings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("binding_id", sa.String(64), unique=True, nullable=False),
        sa.Column(
            "integration_pk",
            sa.Integer(),
            sa.ForeignKey("project_integrations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "task_pk",
            sa.Integer(),
            sa.ForeignKey("test_tasks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("event_filter_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("parameter_mapping_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "integration_pk", "task_pk",
            name="uq_ci_trigger_bindings_integration_task",
        ),
    )
    op.create_index(
        "ix_ci_trigger_bindings_integration_enabled",
        "ci_trigger_bindings",
        ["integration_pk", "enabled"],
    )

    op.create_table(
        "ci_webhook_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "integration_pk",
            sa.Integer(),
            sa.ForeignKey("project_integrations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("delivery_id", sa.String(128), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="accepted"),
        sa.Column("triggered_run_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("error_message", sa.String(1024), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "integration_pk", "delivery_id",
            name="uq_ci_webhook_deliveries_integration_delivery",
        ),
    )
    op.create_index(
        "ix_ci_webhook_deliveries_integration_received",
        "ci_webhook_deliveries",
        ["integration_pk", "received_at"],
    )


def downgrade() -> None:
    op.drop_table("ci_webhook_deliveries")
    op.drop_table("ci_trigger_bindings")
    op.drop_table("project_integrations")
