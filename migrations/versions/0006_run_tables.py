"""Run 执行域表（P3.4）。

- task_runs：一次执行快照（script_ref/case_selection/split_policy 固化），
  trigger_type CHECK、status CHECK；引用 task（FK RESTRICT）。
- run_shards：插件 split_shards 分割产出，(run_pk, shard_index) 唯一。
- shard_attempts：派发尝试，(shard_pk, attempt_no) 唯一，历史全量保留（D-20）。
- run_case_results：case 级结果，(run_pk, shard_pk, case_key, attempt_no) 唯一（D-20）。
- run_artifacts：结束产物引用（report/log_archive/data），文件存 data/artifacts/。
- results：Run 级汇总投影，run_pk 唯一（重定位为 Run 级汇总）。

索引与唯一约束按 §6.3。

Revision ID: 0006_run_tables
Revises: 0005_test_tasks
Create Date: 2026-08-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from master.adapters.sqlalchemy.orm.base import JSONType, UTCDateTime

revision: str = "0006_run_tables"
down_revision: Union[str, None] = "0005_test_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("project_pk", sa.Integer(), nullable=False),
        sa.Column("task_pk", sa.Integer(), nullable=False),
        sa.Column("script_ref", JSONType, nullable=False),
        sa.Column("case_selection", JSONType, nullable=False),
        sa.Column("split_policy", JSONType, nullable=False),
        sa.Column("trigger_type", sa.String(length=16), nullable=False),
        sa.Column("triggered_by_user_pk", sa.Integer(), nullable=True),
        sa.Column("integration_id", sa.String(length=64), nullable=True),
        sa.Column("trigger_context", JSONType, nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=True),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "trigger_type IN ('manual_web','api','schedule','ci_webhook',"
            "'retry','recovery')",
            name="ck_task_runs_trigger_type",
        ),
        sa.CheckConstraint(
            "status IN ('created','dispatched','acked','running','succeeded',"
            "'failed','cancelled','timed_out','lost')",
            name="ck_task_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["project_pk"], ["projects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_pk"], ["test_tasks.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["triggered_by_user_pk"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_task_runs_run_id", "task_runs", ["run_id"], unique=True)
    op.create_index("ix_task_runs_project_pk", "task_runs", ["project_pk"])
    op.create_index("ix_task_runs_task_pk", "task_runs", ["task_pk"])
    op.create_index(
        "ix_task_runs_project_status_created",
        "task_runs",
        ["project_pk", "status", "created_at"],
    )
    op.create_index(
        "ix_task_runs_project_trigger_created",
        "task_runs",
        ["project_pk", "trigger_type", "created_at"],
    )
    op.create_index(
        "ix_task_runs_task_created", "task_runs", ["task_pk", "created_at"]
    )

    op.create_table(
        "run_shards",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shard_id", sa.String(length=64), nullable=False),
        sa.Column("run_pk", sa.Integer(), nullable=False),
        sa.Column("shard_index", sa.Integer(), nullable=False),
        sa.Column("case_keys", JSONType, nullable=False),
        sa.Column("estimated_duration_s", sa.Float(), nullable=True),
        sa.Column("mutex_keys", JSONType, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("final_node", sa.String(length=64), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','dispatching','running','waiting_recovery',"
            "'succeeded','failed','cancelled','timed_out')",
            name="ck_run_shards_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_pk"], ["task_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shard_id"),
        sa.UniqueConstraint(
            "run_pk", "shard_index", name="uq_run_shards_run_index"
        ),
    )
    op.create_index("ix_run_shards_shard_id", "run_shards", ["shard_id"], unique=True)
    op.create_index("ix_run_shards_run_pk", "run_shards", ["run_pk"])
    op.create_index("ix_run_shards_run_status", "run_shards", ["run_pk", "status"])

    op.create_table(
        "shard_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        sa.Column("shard_pk", sa.Integer(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", UTCDateTime(), nullable=True),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('created','dispatched','acked','running','succeeded',"
            "'failed','cancelled','timed_out')",
            name="ck_shard_attempts_status",
        ),
        sa.ForeignKeyConstraint(
            ["shard_pk"], ["run_shards.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id"),
        sa.UniqueConstraint(
            "shard_pk", "attempt_no", name="uq_shard_attempts_shard_attempt"
        ),
    )
    op.create_index(
        "ix_shard_attempts_attempt_id", "shard_attempts", ["attempt_id"], unique=True
    )
    op.create_index("ix_shard_attempts_shard_pk", "shard_attempts", ["shard_pk"])

    op.create_table(
        "run_case_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_pk", sa.Integer(), nullable=False),
        sa.Column("shard_pk", sa.Integer(), nullable=False),
        sa.Column("case_key", sa.String(length=256), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("detail", JSONType, nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','running','passed','failed','skipped','error')",
            name="ck_run_case_results_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_pk"], ["task_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["shard_pk"], ["run_shards.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_pk",
            "shard_pk",
            "case_key",
            "attempt_no",
            name="uq_run_case_results_run_shard_case_attempt",
        ),
    )
    op.create_index("ix_run_case_results_run_pk", "run_case_results", ["run_pk"])
    op.create_index("ix_run_case_results_shard_pk", "run_case_results", ["shard_pk"])

    op.create_table(
        "run_artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("run_pk", sa.Integer(), nullable=False),
        sa.Column("shard_pk", sa.Integer(), nullable=True),
        sa.Column("node_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("file_ref", sa.String(length=512), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('report','log_archive','data')",
            name="ck_run_artifacts_kind",
        ),
        sa.ForeignKeyConstraint(
            ["run_pk"], ["task_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["shard_pk"], ["run_shards.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id"),
    )
    op.create_index(
        "ix_run_artifacts_artifact_id", "run_artifacts", ["artifact_id"], unique=True
    )
    op.create_index("ix_run_artifacts_run_pk", "run_artifacts", ["run_pk"])
    op.create_index("ix_run_artifacts_run_kind", "run_artifacts", ["run_pk", "kind"])

    op.create_table(
        "results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("result_id", sa.String(length=64), nullable=False),
        sa.Column("run_pk", sa.Integer(), nullable=False),
        sa.Column("project_pk", sa.Integer(), nullable=False),
        sa.Column("task_pk", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("metrics", JSONType, nullable=True),
        sa.Column("data", JSONType, nullable=True),
        sa.Column("started_at", UTCDateTime(), nullable=True),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('created','dispatched','acked','running','succeeded',"
            "'failed','cancelled','timed_out','lost')",
            name="ck_results_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_pk"], ["task_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_pk"], ["projects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_pk"], ["test_tasks.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("result_id"),
        sa.UniqueConstraint("run_pk", name="uq_results_run"),
    )
    op.create_index("ix_results_result_id", "results", ["result_id"], unique=True)
    op.create_index("ix_results_run_pk", "results", ["run_pk"])
    op.create_index("ix_results_project_pk", "results", ["project_pk"])
    op.create_index("ix_results_task_pk", "results", ["task_pk"])


def downgrade() -> None:
    op.drop_index("ix_results_task_pk", table_name="results")
    op.drop_index("ix_results_project_pk", table_name="results")
    op.drop_index("ix_results_run_pk", table_name="results")
    op.drop_index("ix_results_result_id", table_name="results")
    op.drop_table("results")

    op.drop_index("ix_run_artifacts_run_kind", table_name="run_artifacts")
    op.drop_index("ix_run_artifacts_run_pk", table_name="run_artifacts")
    op.drop_index("ix_run_artifacts_artifact_id", table_name="run_artifacts")
    op.drop_table("run_artifacts")

    op.drop_index("ix_run_case_results_shard_pk", table_name="run_case_results")
    op.drop_index("ix_run_case_results_run_pk", table_name="run_case_results")
    op.drop_table("run_case_results")

    op.drop_index("ix_shard_attempts_shard_pk", table_name="shard_attempts")
    op.drop_index("ix_shard_attempts_attempt_id", table_name="shard_attempts")
    op.drop_table("shard_attempts")

    op.drop_index("ix_run_shards_run_status", table_name="run_shards")
    op.drop_index("ix_run_shards_run_pk", table_name="run_shards")
    op.drop_index("ix_run_shards_shard_id", table_name="run_shards")
    op.drop_table("run_shards")

    op.drop_index("ix_task_runs_task_created", table_name="task_runs")
    op.drop_index("ix_task_runs_project_trigger_created", table_name="task_runs")
    op.drop_index("ix_task_runs_project_status_created", table_name="task_runs")
    op.drop_index("ix_task_runs_task_pk", table_name="task_runs")
    op.drop_index("ix_task_runs_project_pk", table_name="task_runs")
    op.drop_index("ix_task_runs_run_id", table_name="task_runs")
    op.drop_table("task_runs")
