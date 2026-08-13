"""可靠消息与审计表（P3.5）。

- inbox_messages：入站去重，(origin_id, message_id) 唯一（幂等）。
- outbox_messages：事务性 outbox，status/qos CHECK，(status, next_attempt_at) 索引。
- domain_events：不可变领域事件，sequence 全局唯一（事件顺序）。
- audit_logs：append-only 审计日志，无 FK（actor/project 删除不影响留存）。

Revision ID: 0007_messaging
Revises: 0006_run_tables
Create Date: 2026-08-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from master.adapters.sqlalchemy.orm.base import JSONType, UTCDateTime

revision: str = "0007_messaging"
down_revision: Union[str, None] = "0006_run_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inbox_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("origin_id", sa.String(length=128), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("message_type", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("received_at", UTCDateTime(), nullable=False),
        sa.Column("processed_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "origin_id", "message_id", name="uq_inbox_messages_origin_message"
        ),
    )

    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("outbox_id", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        sa.Column("topic", sa.String(length=256), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("qos", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", UTCDateTime(), nullable=True),
        sa.Column("sent_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','sending','succeeded','retrying','exhausted','cancelled')",
            name="ck_outbox_messages_status",
        ),
        sa.CheckConstraint("qos IN (0, 1, 2)", name="ck_outbox_messages_qos"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("outbox_id"),
    )
    op.create_index(
        "ix_outbox_messages_outbox_id", "outbox_messages", ["outbox_id"], unique=True
    )
    op.create_index(
        "ix_outbox_messages_status_attempt",
        "outbox_messages",
        ["status", "next_attempt_at"],
    )

    op.create_table(
        "domain_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("occurred_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
        sa.UniqueConstraint("sequence", name="uq_domain_events_sequence"),
    )
    op.create_index(
        "ix_domain_events_event_id", "domain_events", ["event_id"], unique=True
    )
    op.create_index(
        "ix_domain_events_project_sequence",
        "domain_events",
        ["project_id", "sequence"],
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("audit_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("detail", JSONType, nullable=False),
        sa.Column("occurred_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("audit_id"),
    )
    op.create_index("ix_audit_logs_audit_id", "audit_logs", ["audit_id"], unique=True)
    op.create_index(
        "ix_audit_logs_project_occurred", "audit_logs", ["project_id", "occurred_at"]
    )
    op.create_index(
        "ix_audit_logs_actor_occurred", "audit_logs", ["actor_id", "occurred_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_actor_occurred", table_name="audit_logs")
    op.drop_index("ix_audit_logs_project_occurred", table_name="audit_logs")
    op.drop_index("ix_audit_logs_audit_id", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_domain_events_project_sequence", table_name="domain_events")
    op.drop_index("ix_domain_events_event_id", table_name="domain_events")
    op.drop_table("domain_events")

    op.drop_index("ix_outbox_messages_status_attempt", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_outbox_id", table_name="outbox_messages")
    op.drop_table("outbox_messages")

    op.drop_table("inbox_messages")
