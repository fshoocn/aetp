"""当前数据库基线元数据。

当前部署直接从 ORM metadata 初始化当前数据库，不导入其他数据库数据。
这里从已注册元数据复制当前表结构，并移除不属于当前模型的外键。
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.schema import ForeignKeyConstraint

from master.adapters.sqlalchemy.orm import Base

SCHEMA_VERSION = "1"

TABLE_NAMES = frozenset(
    {
        "users",
        "refresh_tokens",
        "nodes",
        "devices",
        "node_sessions",
        "projects",
        "project_members",
        "project_node_bindings",
        "plugin_versions",
        "agent_plugin_desired_versions",
        "agent_plugin_sync_operations",
        "script_definitions",
        "test_tasks",
        "test_task_scripts",
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

def build_metadata() -> MetaData:
    """复制当前表结构并剥离指向未知表的外键。"""
    metadata = MetaData(naming_convention=Base.metadata.naming_convention)
    for table_name in sorted(TABLE_NAMES):
        Base.metadata.tables[table_name].to_metadata(metadata)

    for table in metadata.tables.values():
        for constraint in list(table.constraints):
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            if not any(_foreign_key_table(foreign_key) not in TABLE_NAMES for foreign_key in constraint.elements):
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


METADATA = build_metadata()

__all__ = ["METADATA", "SCHEMA_VERSION", "TABLE_NAMES", "build_metadata"]
