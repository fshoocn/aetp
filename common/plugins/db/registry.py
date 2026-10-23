"""db 注册表 / 安装记录存储——实现已移至公共层 :mod:`common.db_registry`。

插件铁律：插件之间不得直接 import，共享契约 / 存储工具放**非插件公共层**；
本模块仅保留再导出，维持 ``common.plugins.db.registry`` 既有公开导入路径。
"""

from common.db_registry import (
    LOG_TABLE,
    DataDependency,
    OwnerRegistry,
    PluginRecordStore,
    consumers_of,
    find_orphans,
    fingerprint,
    meta_table,
    meta_tables,
    normalize_columns,
    parse_dependencies,
    record_dependencies,
    schema_log_table,
)

__all__: list[str] = [
    "LOG_TABLE",
    "DataDependency",
    "OwnerRegistry",
    "PluginRecordStore",
    "consumers_of",
    "find_orphans",
    "fingerprint",
    "meta_table",
    "meta_tables",
    "normalize_columns",
    "parse_dependencies",
    "record_dependencies",
    "schema_log_table",
]
