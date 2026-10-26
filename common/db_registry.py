"""节点治理表（归属登记 / 插件安装记录）、变更日志与数据依赖声明。

单一库设计：这些状态与业务表同住 ``aetp.db``（每节点唯一数据库）。治理表
**各归其主**（按 ``aetp_{owner}_{logical}`` 命名约定，谁的账记谁名下）：

* **归属登记** ``aetp_db_claims``（db 的账本）：owner 单行——认领的表清单
  （JSON 条目 ``{"name", "fp"}``，逐表携带结构指纹）+ ``keep`` 标记 +
  迁移 ``version`` 记账。登记**先于建表**写入（崩溃后孤儿可回收），drop
  成功后删除；
* **插件安装记录** ``aetp_plugin_manager_records``（plugin-manager 的账本，
  经 Model DSL 归属 ``plugin-manager``）：**标量分列**（name / version /
  entrypoint / enabled / last_error / digest），只有形状各异的配置类字段
  （config / requires / kind / dependencies）按 JSON 文本列存放。由
  plugin-manager 写、db 反查消费方依赖；定位是「丢了重装即可」的台账；
* **变更日志** ``aetp_schema_log`` 是审计**流水**（自增 id、按上限截断）；
  日志查询表（``aetp_log_entries``）归 log 插件。
* **数据依赖声明**：``plugin.json`` 的 ``dependencies`` 字段（消费方静态
  manifest），形状 ``[{"plugin", "data", "requires"}]``，与节点能力标签
  ``requires`` 区分（DESIGN §8）。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, TypedDict, cast

from sqlalchemy import Column, Float, Integer, MetaData, String, Table

#: 归属登记表（db 的账本：owner 单行 → 表清单 JSON（含逐表指纹）+ keep + version）
CLAIMS_TABLE: Final[str] = "aetp_db_claims"
#: 库内结构变更日志表（审计流水，保留上限见 ``[db] schema_log_limit``）
LOG_TABLE: Final[str] = "aetp_schema_log"

#: ``requires`` 声明接受的类型符（SQLAlchemy 类型的归一化词表，见 models.type_token）
TYPE_TOKENS: Final[frozenset[str]] = frozenset(
    {"int", "float", "decimal", "bool", "str", "datetime", "date", "time", "json", "bytes", "uuid"}
)

#: 单条数据依赖声明（``plugin.json`` 的 ``dependencies`` 元素）
class DataDependency(TypedDict):
    #: 依赖的生产方插件 id
    plugin: str
    #: 数据（逻辑表名）；与「节点能力标签 requires」无关
    data: str
    #: 最小数据范围（列 → 类型符）——唯一校验基准，必填
    requires: dict[str, str]


MAX_DEPENDENCIES: Final[int] = 32
MAX_REQUIRES_COLUMNS: Final[int] = 64
_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DATA_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_]{1,63}$")
DEPENDENCY_ERROR: Final[str] = (
    "dependencies 必须是列表，每项含 plugin（插件 id）、data（逻辑表名）与非空的 "
    "requires（列 → 类型符，类型符取自 " + " / ".join(sorted(TYPE_TOKENS)) + "）"
)


def parse_dependencies(value: object) -> list[DataDependency]:
    """校验 ``plugin.json`` 的 ``dependencies``（数据依赖）并归一化。

    抛出:
        TypeError: 形状不对（不是列表 / 列表元素不是对象）。
        ValueError: 内容不合法（统一提示见 ``DEPENDENCY_ERROR``）。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(DEPENDENCY_ERROR)
    items = cast(list[object], value)
    if len(items) > MAX_DEPENDENCIES:
        raise ValueError(f"dependencies 最多 {MAX_DEPENDENCIES} 项")
    dependencies: list[DataDependency] = []
    for item in items:
        if not isinstance(item, dict):
            raise TypeError(DEPENDENCY_ERROR)
        entry = cast(dict[str, object], item)
        plugin = entry.get("plugin")
        data = entry.get("data")
        requires = entry.get("requires")
        if not isinstance(plugin, str) or not _NAME.fullmatch(plugin):
            raise ValueError(DEPENDENCY_ERROR)
        if not isinstance(data, str) or not _DATA_NAME.fullmatch(data):
            raise ValueError(DEPENDENCY_ERROR)
        if not isinstance(requires, dict) or not requires:
            raise ValueError(DEPENDENCY_ERROR)
        declared = cast(dict[object, object], requires)
        if len(declared) > MAX_REQUIRES_COLUMNS:
            raise ValueError(f"requires 最多 {MAX_REQUIRES_COLUMNS} 列（依赖 {plugin}/{data}）")
        normalized: dict[str, str] = {}
        for column, token in declared.items():
            if not isinstance(column, str) or not _DATA_NAME.fullmatch(column):
                raise ValueError(DEPENDENCY_ERROR)
            if not isinstance(token, str) or token not in TYPE_TOKENS:
                raise ValueError(DEPENDENCY_ERROR)
            normalized[column] = token
        dependencies.append({"plugin": plugin, "data": data, "requires": normalized})
    return dependencies


def fingerprint(columns: Iterable[tuple[str, str]]) -> str:
    """结构指纹：规范化子结构（列名排序 + 类型归一）sha256 前 16 位。

    ``columns`` 是 ``(列名, 类型符)`` 序列，**范围**由调用方决定：
    整表 = 全列；消费方 ``requires`` 范围 = 声明列子集（DESIGN §4.5）。
    """
    payload = "\n".join(sorted(f"{name}:{token}" for name, token in columns))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_META = MetaData()

#: 归属登记表（db 的账本）：owner 单行 —— 表清单 JSON（逐表指纹）+ keep + version
claims_table: Table[Any] = Table(
    CLAIMS_TABLE,
    _META,
    Column("owner", String(64), primary_key=True),
    Column("tables", String(4096), nullable=False),
    Column("created_at", Float(), nullable=False),
    Column("keep", Integer(), nullable=False, server_default="0"),
    Column("version", Integer(), nullable=True),
)

#: 插件安装记录表（plugin-manager 的账本）：标量分列，形状各异的配置类
#: 字段（config / requires / kind / dependencies）按 JSON 文本列存放。与
#: Model 侧 ``common.plugins.webapi.plugin_models.InstallRecord`` 同名同形
plugin_records_table: Table[Any] = Table(
    "aetp_plugin_manager_records",
    _META,
    Column("id", String(64), primary_key=True),
    Column("name", String(120), nullable=False, server_default=""),
    Column("version", String(64), nullable=False, server_default=""),
    Column("entrypoint", String(160), nullable=False, server_default=""),
    Column("resource_root", String(240), nullable=True),
    Column("enabled", Integer(), nullable=False, server_default="0"),
    Column("last_error", String(2048), nullable=True),
    Column("digest", String(64), nullable=False, server_default=""),
    Column("config", String(4096), nullable=False, server_default="{}"),
    Column("requires", String(2048), nullable=False, server_default="[]"),
    Column("kind", String(64), nullable=False, server_default="[]"),
    Column("dependencies", String(4096), nullable=False, server_default="[]"),
)

#: 库内结构变更日志表
schema_log_table: Table[Any] = Table(
    LOG_TABLE,
    _META,
    Column("id", Integer(), primary_key=True, autoincrement=True),
    Column("ts", Float(), nullable=False),
    Column("owner", String(64), nullable=False, index=True),
    Column("plugin_version", String(64), nullable=False, server_default=""),
    Column("table_name", String(96), nullable=False, server_default=""),
    Column("action", String(32), nullable=False),
    Column("summary", String(2048), nullable=False, server_default=""),
)


def meta_tables() -> tuple[Table[Any], ...]:
    """db 自用治理表（``aetp_db_claims`` / ``aetp_schema_log`` /
    ``aetp_plugin_manager_records``）。"""
    return claims_table, schema_log_table, plugin_records_table


#: 物理表名白名单（纵深防御：拼 DDL 前先过模式校验；正常命名由 models.py 洗名产生）
TABLE_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_]{1,96}")


def find_orphans(owners: Iterable[str], installed: Iterable[str]) -> list[str]:
    """孤儿 owner：归属登记在，但插件已不存在（手删目录 / 清理中途崩溃残留）。"""
    known = set(installed)
    return [owner for owner in owners if owner not in known]


def record_dependencies(record: object) -> list[DataDependency]:
    """从安装记录（``registry.json`` 条目）读 ``dependencies``；损坏条目按空处理。"""
    if not isinstance(record, dict):
        return []
    try:
        return parse_dependencies(cast(dict[str, object], record).get("dependencies"))
    except (TypeError, ValueError):
        return []


def consumers_of(records: Mapping[str, object], owner: str) -> dict[str, list[DataDependency]]:
    """反查依赖某 owner 数据的消费方：``{消费方插件 id: [依赖声明]}``。

    消费方事实源是 ``plugin.json`` 的 ``dependencies``（随安装记录持久化），
    不单独维护第二份登记（DESIGN §4.5 / §8）。
    """
    consumers: dict[str, list[DataDependency]] = {}
    for plugin_id, record in records.items():
        matched = [dep for dep in record_dependencies(record) if dep["plugin"] == owner]
        if matched:
            consumers[str(plugin_id)] = matched
    return consumers


def normalize_columns(columns: Sequence[tuple[str, str]]) -> dict[str, str]:
    """``(列名, 类型符)`` 序列 → 字典（指纹范围归一化用）。"""
    return {name: token for name, token in columns}
