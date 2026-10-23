"""归属登记（``owners.json``）、库内元数据（指纹 / 变更日志 / 版本）与数据依赖声明。

三块持久状态，各有各的落点：

* **归属登记** ``owners.json``（数据目录，原子写）：owner → 认领的物理表。
  登记**先于建表**写入（崩溃后孤儿可回收），drop 成功后删除条目（DESIGN §4.2）；
* **库内元数据**：``aetp_db_meta``（owner → schema_version / 结构指纹）与
  ``aetp_schema_log``（变更日志：时间、变动内容 diff、归属、插件版本）——
  与 DDL 同库，供消费方加载校验与「B 为什么拒载」排障（DESIGN §4.5）；
* **数据依赖声明**：``plugin.json`` 的 ``dependencies`` 字段（消费方静态
  manifest），形状 ``[{"plugin", "data", "requires"}]``，与节点能力标签
  ``requires`` 区分（DESIGN §8）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, TypedDict, cast

from sqlalchemy import Column, Float, Integer, MetaData, String, Table

#: 库内元数据表：owner → （schema_version / 结构指纹）键值对
META_TABLE: Final[str] = "aetp_db_meta"
#: 库内结构变更日志表（保留上限见 ``[db] schema_log_limit``）
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

#: 库内元数据表（owner + 名称 → JSON 值）
meta_table: Table[Any] = Table(
    META_TABLE,
    _META,
    Column("owner", String(64), primary_key=True),
    Column("name", String(128), primary_key=True),
    Column("value", String(4096), nullable=False),
    Column("updated_at", Float(), nullable=False),
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


def meta_tables() -> tuple[Table[Any], Table[Any]]:
    """库内元数据表（``aetp_db_meta`` / ``aetp_schema_log``）。"""
    return meta_table, schema_log_table


#: 物理表名白名单（纵深防御：拼 DDL 前先过模式校验；正常命名由 models.py 洗名产生）
_TABLE_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_]{1,96}")


def _open_store(path: Path) -> sqlite3.Connection:
    """打开（或创建）元数据库连接：WAL + busy_timeout（与业务库同款韧性）。

    元数据操作都是单行级短事务，采用**短连接**（用完即关）：不与调用方
    的生命周期耦合，也不会让文件句柄阻止目录清理。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.DatabaseError:
        # 打开损坏文件时 PRAGMA 失败——先关闭句柄再抛，避免 Windows 文件锁泄漏
        conn.close()
        raise
    return conn


class PluginRecordStore:
    """插件安装记录（``registry.db``）：插件 id → 记录（JSON 文本）。

    schema 版本化（``store_meta.schema_version``）：未来字段增删 bump 版本
    并经迁移升级，不再是"加字段兼容、删字段必崩"的单边容忍；``write`` 全量
    替换在一个事务里完成（原子，崩溃不半截）。
    """

    #: 本程序支持的登记 schema 版本
    SCHEMA_VERSION: Final[str] = "1"

    def __init__(self, path: Path) -> None:
        # 惰性建库：构造不碰文件系统（服务能起），损坏仅在读写时显式报错
        self.path = path

    def _ensure(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS store_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS plugin_records (id TEXT PRIMARY KEY, record TEXT NOT NULL)"
            )
            conn.commit()
            self._check_version(conn)
        except sqlite3.Error as exc:
            raise RuntimeError(f"插件注册表无法初始化：{self.path}：{exc}") from exc

    @staticmethod
    def _check_version(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT value FROM store_meta WHERE name = 'schema_version'"
        ).fetchone()
        if row is None:
            with conn:
                conn.execute(
                    "INSERT INTO store_meta (name, value) VALUES ('schema_version', ?)",
                    (PluginRecordStore.SCHEMA_VERSION,),
                )
            return
        if str(row[0]) != PluginRecordStore.SCHEMA_VERSION:
            raise RuntimeError(
                f"插件注册表 schema 版本不兼容：{row[0]}（本程序支持 {PluginRecordStore.SCHEMA_VERSION}）"
            )

    def read(self) -> dict[str, object]:
        """全部记录（id → 记录对象）；库损坏 / 不可读抛 ``RuntimeError``。"""
        try:
            with contextlib.closing(_open_store(self.path)) as conn:
                self._ensure(conn)
                rows = conn.execute("SELECT id, record FROM plugin_records").fetchall()
            return {str(key): json.loads(str(raw)) for key, raw in rows}
        except (sqlite3.Error, ValueError, RecursionError) as exc:
            raise RuntimeError(f"插件注册表无法读取：{self.path}：{exc}") from exc

    def write(self, records: Mapping[str, object]) -> None:
        """全量替换（单事务原子）；失败抛 ``OSError``（与历史调用方约定一致）。"""
        try:
            with contextlib.closing(_open_store(self.path)) as conn:
                self._ensure(conn)
                with conn:
                    conn.execute("DELETE FROM plugin_records")
                    conn.executemany(
                        "INSERT INTO plugin_records (id, record) VALUES (?, ?)",
                        [
                            (str(key), json.dumps(value, ensure_ascii=False))
                            for key, value in records.items()
                        ],
                    )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OSError(f"插件注册表无法写入：{self.path}：{exc}") from exc


class OwnerRegistry:
    """归属登记（``owners.json``，原子写）：owner → 认领的物理表。

    条目形状（DESIGN §4.2）::

        {"test-runner": {"tables": ["aetp_test_runner_test_run"], "created_at": 1760000000.0}}

    ``keep: true`` 表示卸载时显式保留数据（``keep_data``），孤儿回收跳过。
    """

    def __init__(self, path: Path) -> None:
        # 惰性建库：构造不碰文件系统，损坏仅在读写时显式报错
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        """短连接 + 幂等建表（每操作一次，用完即关）。"""
        conn = _open_store(self.path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS owners ("
                "owner TEXT PRIMARY KEY, tables TEXT NOT NULL, "
                "created_at REAL NOT NULL, keep INTEGER NOT NULL DEFAULT 0)"
            )
            conn.commit()
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    # -- 查询 ---------------------------------------------------------------
    def owners(self) -> list[str]:
        """全部已登记 owner。"""
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute("SELECT owner FROM owners ORDER BY owner").fetchall()
        return [str(row[0]) for row in rows]

    def _tables_of(self, owner: str) -> list[str]:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT tables FROM owners WHERE owner = ?", (owner,)
            ).fetchone()
        if row is None:
            return []
        return [str(name) for name in cast(list[object], json.loads(str(row[0])))]

    def tables(self, owner: str) -> list[str]:
        """某 owner 认领的物理表名。"""
        return self._tables_of(owner)

    def is_kept(self, owner: str) -> bool:
        """该 owner 是否显式保留数据（``keep_data`` 卸载，孤儿回收跳过）。"""
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT keep FROM owners WHERE owner = ?", (owner,)
            ).fetchone()
        return bool(row[0]) if row is not None else False

    # -- 变更（每次操作一个短事务）------------------------------------------
    def claim(self, owner: str, table: str) -> None:
        """登记认领（幂等）；**登记先于建表**写入，崩溃后孤儿可回收。"""
        if not _TABLE_NAME.fullmatch(table):
            raise ValueError(f"表名不合法：{table!r}")
        with contextlib.closing(self._connect()) as conn, conn:
            tables = self._tables_of(owner)
            if table in tables:
                return
            tables.append(table)
            payload = json.dumps(tables, ensure_ascii=False)
            row = conn.execute(
                "SELECT created_at FROM owners WHERE owner = ?", (owner,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO owners (owner, tables, created_at, keep) VALUES (?, ?, ?, 0)",
                    (owner, payload, time.time()),
                )
            else:
                conn.execute(
                    "UPDATE owners SET tables = ? WHERE owner = ?", (payload, owner)
                )

    def drop_table(self, owner: str, table: str) -> None:
        """移除单表登记（drop 成功后调用）。"""
        with contextlib.closing(self._connect()) as conn, conn:
            tables = self._tables_of(owner)
            if table not in tables:
                return
            tables.remove(table)
            conn.execute(
                "UPDATE owners SET tables = ? WHERE owner = ?",
                (json.dumps(tables, ensure_ascii=False), owner),
            )

    def set_keep(self, owner: str, keep: bool) -> None:
        """标记 / 取消「保留数据」（``keep_data`` 卸载后孤儿回收跳过）。"""
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE owners SET keep = ? WHERE owner = ?", (1 if keep else 0, owner)
            )

    def release(self, owner: str) -> None:
        """删除 owner 的整条登记（全部表 drop 成功后调用）。"""
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM owners WHERE owner = ?", (owner,))

    # （旧 json 实现已迁移至 sqlite，见上）


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
