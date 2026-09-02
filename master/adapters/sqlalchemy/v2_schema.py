"""V2-only 数据库基线元数据。

V2 部署使用独立数据库，不执行历史 Alembic 链，也不导入旧数据库数据。
ORM 类仍由共享运行时使用，因此这里从已注册元数据复制表结构，并移除
只服务于 V1 的 legacy 外键。
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.schema import ForeignKeyConstraint

from master.adapters.sqlalchemy.orm import Base

V2_SCHEMA_VERSION = "1"

V2_TABLE_NAMES = frozenset(
    {
        "users",
        "refresh_tokens",
        "nodes",
        "node_sessions",
        "projects",
        "project_members",
        "project_node_bindings",
        "plugin_versions",
        "agent_plugin_desired_versions",
        "agent_plugin_sync_operations",
        "script_definitions",
        "v2_test_tasks",
        "v2_test_task_scripts",
        "task_runs",
        "run_shards",
        "shard_attempts",
        "run_case_results",
        "run_artifacts",
        "run_logs",
        "results",
        "execution_plans",
        "resource_leases",
        "outbox_messages",
        "inbox_messages",
        "domain_events",
        "agent_log_events",
        "agent_maintenance_operations",
        "node_maintenance_locks",
        "node_capability_snapshots",
        "agent_diagnostics_snapshots",
        "notification_endpoints",
        "event_subscriptions",
        "event_deliveries",
        "secret_values",
        "audit_logs",
        "hook_executions",
        "task_schedules",
        "project_integrations",
        "ci_trigger_bindings",
        "ci_webhook_deliveries",
        "run_extension_results",
        "idempotency_records",
    }
)

LEGACY_TABLE_NAMES = frozenset(
    {
        "tasks",
        "task_logs",
        "test_scripts",
        "script_cases",
        "test_tasks",
    }
)


def build_v2_metadata() -> MetaData:
    """复制 V2 表结构并剥离指向 legacy 表的外键。"""
    metadata = MetaData(naming_convention=Base.metadata.naming_convention)
    for table_name in sorted(V2_TABLE_NAMES):
        Base.metadata.tables[table_name].to_metadata(metadata)

    for table in metadata.tables.values():
        for constraint in list(table.constraints):
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            if not any(_foreign_key_table(foreign_key) not in V2_TABLE_NAMES for foreign_key in constraint.elements):
                continue
            table.constraints.remove(constraint)
            for foreign_key in constraint.elements:
                if foreign_key in table.foreign_keys:
                    table.foreign_keys.remove(foreign_key)
                if foreign_key in foreign_key.parent.foreign_keys:
                    foreign_key.parent.foreign_keys.remove(foreign_key)
    return metadata


def _foreign_key_table(foreign_key) -> str:
    return foreign_key.target_fullname.split(".", 1)[0]


V2_METADATA = build_v2_metadata()

__all__ = ["LEGACY_TABLE_NAMES", "V2_METADATA", "V2_SCHEMA_VERSION", "V2_TABLE_NAMES", "build_v2_metadata"]
