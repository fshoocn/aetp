"""db 插件（``common/plugins/db``）测试。

覆盖 DESIGN §9 的关键机制：配置校验与掩码、命名与归属、CRUD / 事务、
生命周期（热更新 / 停机保留、卸载清理）、清理鲁棒性（孤儿回收、被依赖拒绝）、
迁移（L1 加性对齐 / L2 版本续跑与回滚）、跨插件依赖（requires 校验、
未声明拒绝、运行中依赖失效停用）与跨插件联查（external_model）。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import sys
import tempfile
import time
import types
import unittest
import zipfile
from collections.abc import Callable, Mapping, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any, ClassVar, cast
from unittest import mock

from cordis_port import Context
from sqlalchemy import BigInteger, Float, Integer, SmallInteger, String, column, func, literal, select, table, text
from sqlalchemy.exc import SQLAlchemyError

from _helpers import free_port as _free_port
from common.cordis_utils import unload_all
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.db import (
    DataDependency,
    DbConfig,
    DbDependencyError,
    DbError,
    DbPlugin,
    DbService,
    Mapped,
    Model,
    mapped_column,
)
from common.plugins.db.models import (
    compact,
    decode_plugin_id,
    model_logical,
    model_owner,
    module_owner,
    physical_name,
    snake,
)
from common.plugins.db.registry import OwnerRegistry, PluginRecordStore, fingerprint, parse_dependencies
from common.plugins.db.service import sqlite_pool_warning, wider_than
from common.plugins.webapi import WebApiPlugin
from common.plugins.webapi.plugin_manager import PluginManagerError, PublicPluginRecord
from common.plugins.webapi.plugin_manager_plugin import PluginManagerPlugin


# ---------------------------------------------------------------- 模块级模型
class RunRecord(Model):
    """归属 test-store 的执行记录（逻辑表名 test_run）。"""

    __owner__ = "test-store"
    __tablename__ = "test_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    kind: Mapped[str] = mapped_column(default="manual")
    payload: Mapped[dict[str, Any] | None]


class Report(Model):
    """归属 reporter 的报表行（联查 external test_run 用）。"""

    __owner__ = "reporter"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int]
    kind: Mapped[str]


# ---------------------------------------------------------------- 测试辅助
def _synthetic_package(owner: str) -> str:
    """插件管理器的合成包名约定（``_aetp_plugin_<插件 id 的 ascii hex>``）。"""
    return "_aetp_plugin_" + owner.encode("ascii").hex()


def _synthetic_import(root: Path, owner: str, source: str) -> types.ModuleType:
    """把模型源写成「合成插件包」并导入（与插件管理器装载插件同约定）。

    模块名 ``_aetp_plugin_<hex>.models`` → 归属解码即插件 id，验证
    「owner 识别」的默认路径；重复调用 = 插件重装重执行模块。
    """
    package = _synthetic_package(owner)
    package_dir = root / package
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    module_path = package_dir / "models.py"
    module_path.write_text(source, encoding="utf-8")
    name = f"{package}.models"
    sys.modules.pop(name, None)  # 模拟插件管理器的 _remove_modules
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载合成模块：{module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _plugin_zip(
    plugin_id: str,
    name: str,
    source: str,
    *,
    dependencies: Sequence[Mapping[str, object]] | None = None,
) -> bytes:
    """内存拼一个最小插件交付包（plugin.json + plugin.py）。"""
    manifest: dict[str, object] = {
        "id": plugin_id,
        "name": name,
        "version": "1.0.0",
        "entrypoint": "plugin:EntryPlugin",
        "kind": "any",
        "requires": [],
    }
    if dependencies is not None:
        manifest["dependencies"] = dependencies
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("plugin.json", json.dumps(manifest, ensure_ascii=False))
        package.writestr("plugin.py", source)
    return buffer.getvalue()


#: 生产方插件 V1：init 里建自己的表（归属 = 插件 id，经合成包解码）
_PRODUCER_SOURCE_V1 = """
from common.plugins.db import Model, Mapped, mapped_column


class Note(Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str]


class EntryPlugin:
    name = "data-producer"
    inject = ["db"]

    def __init__(self, ctx, config=None):
        self.ctx = ctx

    async def init(self):
        await self.ctx.db.sync_schema()
"""

#: 生产方插件 V2：模型改 body + 迁移步骤把 text 改名（模型与迁移同批交付）
_PRODUCER_SOURCE_V2 = """
from sqlalchemy import text

from common.plugins.db import Model, Mapped, mapped_column


class Note(Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str]


async def upgrade_v2(conn):
    await conn.execute(text("ALTER TABLE aetp_data_producer_note RENAME COLUMN text TO body"))


MIGRATIONS = [(2, upgrade_v2)]


class EntryPlugin:
    name = "data-producer"
    inject = ["db"]

    def __init__(self, ctx, config=None):
        self.ctx = ctx

    async def init(self):
        await self.ctx.db.sync_schema(migrations=MIGRATIONS)
"""

_PRODUCER_DEPENDENCIES = [
    {"plugin": "data-producer", "data": "note", "requires": {"id": "int", "text": "str"}}
]


class DbStackTestCase(unittest.IsolatedAsyncioTestCase):
    """最小 db 栈：appconfig + db（临时目录、独立库文件）。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.data_dir = self.root / "db-data"
        self.install_root = self.root / "installed"
        self.config_path = self.root / "config.ini"
        self.config_path.write_text(self._ini(orphan_policy="warn"), encoding="utf-8")
        self.context = Context()
        self.context.provide("node_kind", "master")
        await self.context.plugin(AppConfigPlugin, {"path": str(self.config_path)})
        self.db_fiber = await self.context.plugin(DbPlugin)

    def _ini(self, *, orphan_policy: str) -> str:
        db_file = (self.root / "aetp.db").as_posix()
        return (
            "[node]\n"
            "kind = master\n"
            f"install_root = {self.install_root.as_posix()}\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{db_file}\n"
            f"data_dir = {self.data_dir.as_posix()}\n"
            f"orphan_policy = {orphan_policy}\n"
        )

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self._temp.cleanup()

    @property
    def db(self) -> DbService:
        """``ctx.db`` 服务（测试根 ctx 直调 = 内部调用，无归属校验）。"""
        return cast(DbService, self.context.get("db"))

    def write_registry(self, records: dict[str, object]) -> None:
        """写插件安装记录（``registry.db`` 事实源，依赖校验据此反查）。"""
        self.install_root.mkdir(parents=True, exist_ok=True)
        PluginRecordStore(self.install_root / "registry.db").write(records)


class _CapturePlugin:
    """模拟插件：把 init 里的 ``ctx.db`` 调用结果收进 ClassVar（按测试复用）。

    不吞异常：init 失败即 fiber 加载失败，错误经 ``await fiber`` 传播，
    测试用 ``assertRaises`` 断言类型。
    """

    name: ClassVar[str] = "capture"
    inject: ClassVar[list[str]] = ["db"]
    outcome: ClassVar[list[object]] = []
    action: ClassVar[Callable[[Context], object]]

    def __init__(self, ctx: Context, config: object = None) -> None:
        self.ctx = ctx

    async def init(self) -> None:
        result = type(self).action(self.ctx)
        if inspect.isawaitable(result):
            result = await result
        type(self).outcome.append(result)


def _capture_plugin(name: str, action: Callable[[Context], object]) -> type[_CapturePlugin]:
    """按名字 / 行为生成一个捕获型插件类（fiber 名 = name）。"""
    return type(
        f"Capture_{name.replace('-', '_')}",
        (_CapturePlugin,),
        {"name": name, "action": staticmethod(action), "outcome": []},
    )


# ---------------------------------------------------------------- 配置与命名
class ConfigTests(unittest.TestCase):
    """[db] 配置解析、校验与凭证掩码（DESIGN §6）。"""

    def test_validation_rejects_bad_values(self) -> None:
        result = DbConfig.validate({"orphan_policy": "maybe", "pool_size": 0, "url": "not a url"})
        issues = cast(list[dict[str, object]], result.get("issues", []))
        paths = {str(issue.get("path")) for issue in issues}
        self.assertIn("['orphan_policy']", paths)
        self.assertIn("['pool_size']", paths)
        self.assertIn("['url']", paths)

    def test_password_masked_and_env_resolution(self) -> None:
        config = DbConfig(url="postgresql+asyncpg://user:secret@host/db")
        self.assertEqual(config.to_dict()["url"], "postgresql+asyncpg://user:***@host/db")
        self.assertEqual(config.raw_dict()["url"], "postgresql+asyncpg://user:secret@host/db")
        # 掩码不落盘：ini 写回仍是原始凭证
        self.assertEqual(DbConfig.to_ini(config)["url"], "postgresql+asyncpg://user:secret@host/db")

        import os

        os.environ["AETP_TEST_DB_URL"] = "sqlite+aiosqlite:///data/env.db"
        try:
            env_config = DbConfig(url="env:AETP_TEST_DB_URL")
            self.assertEqual(env_config.resolved_url(), "sqlite+aiosqlite:///data/env.db")
            with self.assertRaises(ValueError):
                DbConfig(url="env:AETP_TEST_DB_MISSING").resolved_url()
        finally:
            del os.environ["AETP_TEST_DB_URL"]

    def test_naming_and_type_tokens(self) -> None:
        self.assertEqual(snake("TestRun"), "test_run")
        self.assertEqual(physical_name("test-runner", "test_run"), "aetp_test_runner_test_run")
        long_a = physical_name("very-long-owner-name-here", "very_long_logical_table_name")
        long_b = physical_name("very-long-owner-name-here", "very_long_logical_table_name2")
        self.assertLessEqual(len(long_a), 30)
        self.assertNotEqual(long_a, long_b)  # 超长截断 + hash 防撞
        self.assertEqual(compact("short_name"), "short_name")
        self.assertEqual(decode_plugin_id("_aetp_plugin_74657374"), "test")
        self.assertEqual(module_owner("common.plugins.x"), "common")
        self.assertEqual(fingerprint([("a", "int"), ("b", "str")]), fingerprint([("b", "str"), ("a", "int")]))

    def test_dependency_manifest_parsing(self) -> None:
        deps = parse_dependencies(
            [{"plugin": "test-store", "data": "test_run", "requires": {"id": "int", "name": "str"}}]
        )
        self.assertEqual(deps[0]["plugin"], "test-store")
        bad_cases: tuple[object, ...] = (
            [{"plugin": "x", "data": "y"}],  # 缺 requires
            [{"plugin": "x", "data": "y", "requires": dict[str, str]()}],  # 空 requires
            [{"plugin": "x", "data": "y", "requires": {"id": "weird"}}],  # 非法类型符
            {"not": "a list"},  # 形状错误
        )
        for bad in bad_cases:
            with self.assertRaises((TypeError, ValueError)):
                parse_dependencies(bad)


class ModelRegistryTests(unittest.TestCase):
    """模型注册：逻辑表名冲突、重装幂等（DESIGN §11-3）。"""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_failed_definition_releases_logical_name(self) -> None:
        """DB-N10：定义期失败（外键解析失败）回滚登记，同逻辑表名可重新定义。"""
        bad = (
            "from sqlalchemy import ForeignKey\n"
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Broken(Model):\n"
            "    __tablename__ = 'n10_probe'\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    ref: Mapped[int] = mapped_column(ForeignKey('ghost_table.id'))\n"
        )
        with self.assertRaises(SQLAlchemyError):
            _synthetic_import(self.root, "n10-owner", bad)

        retry = (
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Retry(Model):\n"
            "    __tablename__ = 'n10_probe'\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
        )
        module = _synthetic_import(self.root, "n10-owner", retry)
        self.assertEqual(module.Retry.__model_logical__, "n10_probe")

    def test_logical_name_conflict_rejected(self) -> None:
        source = (
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class DupA(Model):\n"
            "    __tablename__ = 'dup'\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "class DupB(Model):\n"
            "    __tablename__ = 'dup'\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
        )
        with self.assertRaises(ValueError):
            _synthetic_import(self.root, "dup-owner", source)

    def test_reload_replaces_group_idempotently(self) -> None:
        v1 = (
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Item(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    name: Mapped[str]\n"
        )
        first = _synthetic_import(self.root, "reload-shop", v1)
        self.assertEqual(first.Item.__table__.name, "aetp_reload_shop_item")
        self.assertEqual(model_logical(first.Item), "item")
        second = _synthetic_import(self.root, "reload-shop", v1)  # 重装：重执行模块
        self.assertEqual(second.Item.__table__.name, "aetp_reload_shop_item")
        self.assertIsNot(first.Item, second.Item)

    def test_synthetic_module_decodes_owner(self) -> None:
        module = _synthetic_import(
            self.root,
            "owner-probe",
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Probe(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n",
        )
        self.assertEqual(module.Probe.__model_owner__, "owner-probe")
        self.assertEqual(_synthetic_package("owner-probe"), module.__name__.rsplit(".", 1)[0])


# ---------------------------------------------------------------- CRUD 与事务
class DbServiceTests(DbStackTestCase):
    """CRUD / 事务 / 生命周期 / 清理（DESIGN §3、§4.3、§7）。"""

    async def test_crud_roundtrip(self) -> None:
        db = self.db
        runs = db.repo(RunRecord)
        result = await db.sync_schema(models=[RunRecord])
        self.assertEqual(result.created, ["aetp_test_store_test_run"])

        created = await runs.add(RunRecord(name="nightly", payload={"a": 1}))
        self.assertIsNotNone(created.id)
        fetched = await runs.get(created.id)
        assert fetched is not None
        self.assertEqual(fetched.name, "nightly")
        self.assertEqual(await runs.count(), 1)
        rows = await runs.list(where=(RunRecord.name == "nightly"), limit=10)
        self.assertEqual([row.payload for row in rows], [{"a": 1}])
        self.assertEqual(await runs.update(created.id, {"name": "renamed"}), 1)
        renamed = await runs.get(created.id)
        assert renamed is not None
        self.assertEqual(renamed.name, "renamed")
        self.assertEqual(await runs.delete(created.id), 1)
        self.assertEqual(await runs.count(), 0)

    async def test_transaction_rollback_and_savepoint(self) -> None:
        db = self.db
        await db.sync_schema(models=[RunRecord])
        runs = db.repo(RunRecord)

        with self.assertRaises(RuntimeError):
            async with db.transaction():
                await runs.add(RunRecord(name="doomed"))
                raise RuntimeError("boom")
        self.assertEqual(await runs.count(), 0)

        async with db.session() as session:
            session.add(RunRecord(name="outer"))
            await session.flush()
            with self.assertRaises(RuntimeError):
                async with db.transaction(session):  # 嵌套 = SAVEPOINT
                    session.add(RunRecord(name="inner"))
                    await session.flush()
                    raise RuntimeError("rollback inner only")
        names = {row.name for row in await runs.list()}
        self.assertEqual(names, {"outer"})

    async def test_hot_update_keeps_data(self) -> None:
        db = self.db
        await db.sync_schema(models=[RunRecord])
        await db.repo(RunRecord).add(RunRecord(name="keep-me"))

        outcome = self.db_fiber.update({"echo": False}, no_save=True)
        assert outcome is not None  # 热更新必须真的发生：拿不到结果不许悄悄跳过
        await outcome
        refreshed = self.context.get("db")
        self.assertEqual(await refreshed.repo(RunRecord).count(), 1)  # 热更新不动表

    async def test_drop_owner_and_keep_data(self) -> None:
        db = self.db
        await db.sync_schema(models=[RunRecord])
        await db.repo(RunRecord).add(RunRecord(name="row"))

        await db.drop_owner("test-store", keep_data=True)
        self.assertEqual(await db.repo(RunRecord).count(), 1)  # keep_data 保留
        owners = OwnerRegistry(self.data_dir / "owners.sqlite")
        self.assertTrue(owners.is_kept("test-store"))

        await db.drop_owner("test-store")
        result = await db.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        self.assertNotIn("aetp_test_store_test_run", {row[0] for row in result})
        self.assertEqual(OwnerRegistry(self.data_dir / "owners.sqlite").tables("test-store"), [])

    async def test_drop_owner_rejects_cross_owner_caller(self) -> None:
        """DB-H1：业务插件不能清理别的 owner 的数据（只能清自己的归属）。"""
        db = self.db
        await db.sync_schema(models=[RunRecord])
        outcome: list[str] = []

        class RoguePlugin:
            name: ClassVar[str] = "rogue-drop"
            inject: ClassVar[list[str]] = ["db"]

            def __init__(self, ctx: Context, config: object) -> None:
                del config
                self.ctx = ctx

            async def init(self) -> None:
                try:
                    await self.ctx.db.drop_owner(model_owner(RunRecord), force=True)
                    outcome.append("dropped")
                except DbError as exc:
                    outcome.append(str(exc))

        await self.context.plugin(RoguePlugin)
        self.assertTrue(outcome and "越权" in outcome[0], outcome)
        # 表未被动过，数据仍可写
        await db.repo(RunRecord).add(RunRecord(name="alive"))
        self.assertEqual(await db.repo(RunRecord).count(), 1)

    async def test_wait_for_data(self) -> None:
        db = self.db
        self.assertFalse(await db.wait_for_data("test-store", "test_run", timeout=0.05))
        await db.sync_schema(models=[RunRecord])
        self.assertTrue(await db.wait_for_data("test-store", "test_run", timeout=1.0))

    async def test_concurrent_sync_schema_serialized(self) -> None:
        db = self.db
        results = await asyncio.gather(
            db.sync_schema(models=[RunRecord]),
            db.sync_schema(models=[RunRecord]),
        )
        self.assertEqual(results[0].created + results[1].created, ["aetp_test_store_test_run"])

    async def test_owner_mismatch_and_explicit_owner(self) -> None:
        db = self.db
        await db.sync_schema(models=[RunRecord])
        intruder = _capture_plugin("intruder", lambda ctx: ctx.get("db").repo(RunRecord))
        with self.assertRaises(DbError):  # 归属校验失败经 fiber 传播
            await self.context.plugin(intruder)

        # 显式 owner= 视为跨插件协作的显式授权
        good_cls = _capture_plugin("intruder2", lambda ctx: ctx.get("db").repo(RunRecord, owner="test-store"))
        await self.context.plugin(good_cls)
        self.assertEqual(len(good_cls.outcome), 1)


# ---------------------------------------------------------------- 迁移
class SchemaLogTests(DbStackTestCase):
    """DB-N9：变更日志按 owner 公平裁剪。"""

    def _ini(self, *, orphan_policy: str) -> str:
        return super()._ini(orphan_policy=orphan_policy) + "schema_log_limit = 2\n"

    async def test_log_trim_is_fair_across_owners(self) -> None:
        """日志多的 owner 不挤掉别人的：各自保最新 limit 条。"""
        db = self.db
        versions_a = [
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Item(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            f"{extra}"
            for extra in (
                "",
                "    c1: Mapped[str | None]\n",
                "    c1: Mapped[str | None]\n    c2: Mapped[str | None]\n",
                "    c1: Mapped[str | None]\n    c2: Mapped[str | None]\n    c3: Mapped[str | None]\n",
            )
        ]
        versions_b = [
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Row(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            f"{extra}"
            for extra in (
                "",
                "    d1: Mapped[str | None]\n",
                "    d1: Mapped[str | None]\n    d2: Mapped[str | None]\n",
                "    d1: Mapped[str | None]\n    d2: Mapped[str | None]\n    d3: Mapped[str | None]\n",
            )
        ]
        for version in versions_a:
            module = _synthetic_import(self.root / "synthetic", "log-a", version)
            await db.sync_schema(models=[module.Item])
        for version in versions_b:
            module = _synthetic_import(self.root / "synthetic", "log-b", version)
            await db.sync_schema(models=[module.Row])

        rows = await db.execute(
            text("SELECT owner, COUNT(*) FROM aetp_schema_log GROUP BY owner")
        )
        counts = {str(row[0]): int(row[1]) for row in rows}
        self.assertEqual(counts, {"log-a": 2, "log-b": 2})


class RegistrySweepSafetyTests(DbStackTestCase):
    """DB-N3：孤儿回收的前置条件 = registry 事实源可读。"""

    def _ini(self, *, orphan_policy: str) -> str:
        del orphan_policy
        return super()._ini(orphan_policy="drop")

    async def test_corrupt_registry_skips_sweep_but_clean_registry_sweeps(self) -> None:
        """registry 损坏 → 整体跳过（数据保留）；可读且不含该 owner → 照常清理。"""
        db = self.db
        await db.sync_schema(models=[RunRecord])
        registry_path = self.install_root / "registry.db"
        self.install_root.mkdir(parents=True, exist_ok=True)

        # registry 损坏：残缺 id 集合不得当“已安装全集”，孤儿回收整体跳过
        registry_path.write_bytes(b"this is not a sqlite database")
        outcome = self.db_fiber.update({"echo": False}, no_save=True)
        assert outcome is not None
        await outcome
        await self.db.repo(RunRecord).add(RunRecord(name="alive"))

        # 对照：registry 可读且不含该 owner → 按 orphan_policy=drop 清理
        registry_path.unlink()
        PluginRecordStore(registry_path).write({})
        outcome = self.db_fiber.update({"echo": False}, no_save=True)
        assert outcome is not None
        await outcome
        with self.assertRaises((DbError, SQLAlchemyError)):
            await self.db.repo(RunRecord).add(RunRecord(name="ghost"))


class InstallRootResolutionTests(unittest.IsolatedAsyncioTestCase):
    """[node] install_root 相对路径按节点目录（config.ini 所在目录）解析。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.config_path = self.root / "config.ini"
        self.config_path.write_text(
            "[node]\n"
            "kind = master\n"
            "install_root = plugins\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'aetp.db').as_posix()}\n"
            "orphan_policy = warn\n",
            encoding="utf-8",
        )
        self.context = Context()
        self.context.provide("node_kind", "master")
        await self.context.plugin(AppConfigPlugin, {"path": str(self.config_path)})

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self._temp.cleanup()

    async def test_relative_install_root_anchors_to_config_dir(self) -> None:
        """回归：registry.db 落节点目录下，不随启动时的工作目录漂移。"""
        await self.context.plugin(DbPlugin)
        self.assertTrue((self.root / "plugins" / "registry.db").is_file())


class EngineResilienceTests(DbStackTestCase):
    """DB-N4/N5/N6：逃生舱事务语义、SQLite 并发韧性、池参数告警。"""

    async def test_execute_joins_ambient_transaction(self) -> None:
        """DB-N4：事务内 execute 汇入环境事务——外层回滚时其写入一并回滚。"""
        db = self.db
        await db.sync_schema(models=[RunRecord])
        with self.assertRaises(RuntimeError):
            async with db.transaction():
                await db.execute(
                    text("INSERT INTO aetp_test_store_test_run (name) VALUES ('ghost')")
                )
                raise RuntimeError("boom")
        self.assertEqual(await db.repo(RunRecord).count(), 0)

    async def test_sqlite_uses_wal(self) -> None:
        """DB-N5：SQLite 文件库连接启用 WAL（读写不互斥，配 busy_timeout）。"""
        rows = await self.db.execute(text("PRAGMA journal_mode"))
        self.assertEqual(str(rows[0][0]).lower(), "wal")

    def test_sqlite_pool_warning_flags_ignored_options(self) -> None:
        """DB-N6：SQLite 方言显式配置池参数 → 告警；其他情形不告警。"""
        custom = DbConfig(url="sqlite+aiosqlite:///x.db", pool_size=3)
        self.assertIn("pool_size", sqlite_pool_warning(custom, "sqlite") or "")
        self.assertIsNone(sqlite_pool_warning(custom, "postgresql"))
        self.assertIsNone(
            sqlite_pool_warning(DbConfig(url="sqlite+aiosqlite:///x.db"), "sqlite")
        )


class WiderThanTests(unittest.TestCase):
    """DB-N8：L1 自动放宽判定（字符串长度 + 整数族位宽）。"""

    def test_string_length_and_integer_width(self) -> None:
        self.assertTrue(wider_than(String(10), String(5)))
        self.assertFalse(wider_than(String(5), String(10)))
        self.assertTrue(wider_than(BigInteger(), Integer()))
        self.assertTrue(wider_than(Integer(), SmallInteger()))
        self.assertFalse(wider_than(Integer(), BigInteger()))
        self.assertFalse(wider_than(String(5), Integer()))
        self.assertFalse(wider_than(Float(), Integer()))


class DropOwnerTests(DbStackTestCase):
    """DB-N2：drop_owner 部分失败不整体悬停。"""

    async def test_partial_failure_cleans_successes_and_reports_rest(self) -> None:
        """成功表登记/指纹即时清理；残表可观测可重试，不再卡半删态。"""
        db = self.db
        source = (
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Item(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "class Row(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
        )
        module = _synthetic_import(self.root / "synthetic", "partial", source)
        await db.sync_schema(models=[module.Item, module.Row])
        owner = model_owner(module.Item)
        item_table = physical_name(owner, "item")
        row_table = physical_name(owner, "row")

        async def fake_drop(_self: DbService, table_name: str) -> bool:
            return table_name != row_table  # row 表删不动

        with mock.patch.object(DbService, "_drop_table", fake_drop):
            remaining = await db.drop_owner(owner)
        self.assertEqual(remaining, [row_table])

        # 成功表：登记已摘、指纹已清；残表登记保留（待重试/孤儿回收）
        registry = OwnerRegistry(self.data_dir / "owners.sqlite")
        self.assertEqual(registry.tables(owner), [row_table])
        meta_rows = await db.execute(text("SELECT name FROM aetp_db_meta"))
        names = {str(row[0]) for row in meta_rows}
        self.assertNotIn(f"fp:{item_table}", names)
        self.assertIn(f"fp:{row_table}", names)

        # 残表重试可完成清理（可再次整体清理，不悬停）
        self.assertEqual(await db.drop_owner(owner), [])
        self.assertEqual(OwnerRegistry(self.data_dir / "owners.sqlite").tables(owner), [])


class MigrationTests(DbStackTestCase):
    """L1 加性对齐与 L2 版本化迁移（DESIGN §4.4）。"""

    async def test_migration_versions_must_be_contiguous(self) -> None:
        """DB-N7：迁移版本号需从 2 起连续，跳号直接拒绝。"""
        db = self.db

        async def noop(_conn: Any) -> None:
            pass

        with self.assertRaises(DbError) as caught:
            await db.sync_schema(models=[RunRecord], migrations=[(2, noop), (5, noop)])
        self.assertIn("不连续", str(caught.exception))

    async def test_strict_align_failure_is_isolated(self) -> None:
        """DB-N1：严格补漏失败只影响该表——其他表照常记账，阶段 3 不被跳过。"""
        db = self.db
        v1 = (
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Good(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    name: Mapped[str]\n"
            "class Bad(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
        )
        v2 = (
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Good(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    name: Mapped[str]\n"
            "    note: Mapped[str | None]\n"
            "class Bad(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    required: Mapped[str]\n"
        )
        module = _synthetic_import(self.root / "synthetic", "isolate-sync", v1)
        await db.sync_schema(models=[module.Good, module.Bad])

        reloaded = _synthetic_import(self.root / "synthetic", "isolate-sync", v2)
        with self.assertRaises(DbError) as caught:
            await db.sync_schema(models=[reloaded.Good, reloaded.Bad])
        self.assertIn("required", str(caught.exception))

        # 阶段 3 未被跳过：Good 的加列已记入变更日志
        rows = await db.execute(
            text("SELECT summary FROM aetp_schema_log WHERE table_name LIKE '%good'")
        )
        self.assertTrue(any("add column note" in str(row[0]) for row in rows), rows)

    async def test_l1_add_column_and_index(self) -> None:
        db = self.db
        v1 = (
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Evolve(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    name: Mapped[str]\n"
        )
        v2 = (
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Evolve(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    name: Mapped[str] = mapped_column(index=True)\n"
            "    note: Mapped[str | None]\n"
        )
        module = _synthetic_import(self.root / "synthetic", "migrate-shop", v1)
        await db.sync_schema(models=[module.Evolve])

        reloaded = _synthetic_import(self.root / "synthetic", "migrate-shop", v2)  # 重装重执行
        result = await db.sync_schema(models=[reloaded.Evolve])
        changes = result.altered["aetp_migrate_shop_evolve"]
        self.assertTrue(any(item.startswith("add column note") for item in changes), changes)
        self.assertTrue(any(item.startswith("add index") for item in changes), changes)

    async def test_schema_event_task_isolated_from_ambient_session(self) -> None:
        """DB-H3：事务内 sync_schema 派发的事件，其监听任务不得继承环境会话。"""
        db = self.db
        v1 = (
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Evolve(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    name: Mapped[str]\n"
        )
        v2 = (
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Evolve(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    name: Mapped[str]\n"
            "    note: Mapped[str | None]\n"
        )
        module = _synthetic_import(self.root / "synthetic", "isolate-shop", v1)
        await db.sync_schema(models=[module.Evolve])

        shared: list[bool] = []
        done = asyncio.Event()
        tasks: list[asyncio.Task[None]] = []

        def on_changed(_payload: object) -> None:
            async def probe() -> None:
                async with db.session() as session:
                    # True = 继承到了外层事务的会话（跨任务共用，SQLA 禁止）
                    shared.append(session is outer[0])
                done.set()

            tasks.append(asyncio.create_task(probe()))  # 强引用防 GC（RUF006）

        self.context.on("db/schema-changed", on_changed)
        outer: list[object] = []
        reloaded = _synthetic_import(self.root / "synthetic", "isolate-shop", v2)
        async with db.transaction() as session:
            outer.append(session)
            await db.sync_schema(models=[reloaded.Evolve])
        await asyncio.wait_for(done.wait(), timeout=5)
        self.assertEqual(shared, [False])

    async def test_l2_versioned_migrations_and_rollback(self) -> None:
        db = self.db
        v1 = (
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Evolve(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    name: Mapped[str]\n"
        )
        # 新版插件交付：模型改用 title + 迁移步骤把 name 改名过来（同批发布）
        v2 = (
            "from common.plugins.db import Model, Mapped, mapped_column\n"
            "class Evolve(Model):\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    title: Mapped[str]\n"
        )
        module = _synthetic_import(self.root / "synthetic", "migrate-shop", v1)
        await db.sync_schema(models=[module.Evolve])
        reloaded = _synthetic_import(self.root / "synthetic", "migrate-shop", v2)

        async def upgrade_v2(conn: Any) -> None:
            await conn.execute(text("ALTER TABLE aetp_migrate_shop_evolve RENAME COLUMN name TO title"))

        result = await db.sync_schema(models=[reloaded.Evolve], migrations=[(2, upgrade_v2)])
        self.assertEqual(result.migrated, {"migrate-shop": 2})

        # 已执行的版本不重跑（v2 再跑 rename 会失败）
        again = await db.sync_schema(models=[reloaded.Evolve], migrations=[(2, upgrade_v2)])
        self.assertEqual(again.migrated, {})

        async def broken(conn: Any) -> None:
            await conn.execute(text("UPDATE definitely_missing SET x = 1"))

        with self.assertRaises(DbError):
            await db.sync_schema(models=[reloaded.Evolve], migrations=[(3, broken)])
        version = await db.execute(
            text("SELECT value FROM aetp_db_meta WHERE owner = 'migrate-shop' AND name = 'schema_version'")
        )
        self.assertEqual([row[0] for row in version], ["2"])  # 失败回滚、版本不前移


# ---------------------------------------------------------------- 跨插件依赖
class DependencyTests(DbStackTestCase):
    """requires 校验、未声明拒绝、运行中依赖复检（DESIGN §4.5）。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.db.sync_schema(models=[RunRecord, Report])

    async def test_check_dependencies(self) -> None:
        db = self.db
        good: list[DataDependency] = [
            {"plugin": "test-store", "data": "test_run", "requires": {"id": "int", "name": "str"}}
        ]
        await db.check_dependencies("reporter", good)
        with self.assertRaises(DbDependencyError) as type_mismatch:
            await db.check_dependencies(
                "reporter", [{"plugin": "test-store", "data": "test_run", "requires": {"id": "str"}}]
            )
        self.assertIn("结构已变化", str(type_mismatch.exception))
        with self.assertRaises(DbDependencyError) as missing_table:
            await db.check_dependencies(
                "reporter", [{"plugin": "test-store", "data": "nope", "requires": {"id": "int"}}]
            )
        self.assertIn("不存在", str(missing_table.exception))

    async def test_external_model_requires_declaration(self) -> None:
        self.write_registry({"reporter": {"id": "reporter", "dependencies": []}})
        intruder = _capture_plugin(
            "reporter", lambda ctx: ctx.get("db").external_model("test-store", "test_run")
        )
        with self.assertRaises(DbDependencyError):  # 未声明一律拒绝
            await self.context.plugin(intruder)

        self.write_registry({"reporter": {"id": "reporter", "dependencies": _PRODUCER_LIKE_DEPS}})
        declared = _capture_plugin(
            "reporter", lambda ctx: ctx.get("db").external_model("test-store", "test_run")
        )
        await self.context.plugin(declared)
        self.assertEqual(len(declared.outcome), 1)

    async def test_external_join_and_readonly_repo_rejected(self) -> None:
        db = self.db
        self.write_registry({"reporter": {"id": "reporter", "dependencies": _PRODUCER_LIKE_DEPS}})
        runs = db.repo(RunRecord)
        for name, kind in (("r1", "a"), ("r2", "a"), ("r3", "b")):
            created = await runs.add(RunRecord(name=name, kind=kind))
            await db.repo(Report).add(Report(run_id=created.id, kind=kind))

        plugin_cls = _capture_plugin(
            "reporter", lambda ctx: ctx.get("db").external_model("test-store", "test_run")
        )
        await self.context.plugin(plugin_cls)
        external = cast(Any, plugin_cls.outcome[0])

        # 跨 owner JOIN + GROUP BY 聚合下推
        async with db.session() as session:
            statement = (
                select(Report.kind, func.count())
                .join(external, external.id == Report.run_id)
                .group_by(Report.kind)
            )
            counts = {row[0]: row[1] for row in (await session.execute(statement)).all()}
        self.assertEqual(counts, {"a": 2, "b": 1})

        # 反射映射只读：仓储层拒绝
        with self.assertRaises(DbError):
            db.repo(external)

    async def test_recheck_consumers_reports_failure(self) -> None:
        self.write_registry(
            {
                "reporter": {
                    "id": "reporter",
                    "dependencies": [
                        {"plugin": "test-store", "data": "test_run", "requires": {"nope": "int"}}
                    ],
                }
            }
        )
        failures = await self.db.recheck_consumers("test-store")
        self.assertEqual([consumer for consumer, _ in failures], ["reporter"])
        self.assertIn("结构已变化", failures[0][1])


_PRODUCER_LIKE_DEPS = [
    {"plugin": "test-store", "data": "test_run", "requires": {"id": "int", "name": "str"}}
]


# ---------------------------------------------------------------- 孤儿回收
class OrphanSweepTests(unittest.IsolatedAsyncioTestCase):
    """启动时孤儿回收两策略（DESIGN §4.3 兜底）。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.data_dir = self.root / "db-data"
        self.data_dir.mkdir(parents=True)
        self.contexts: list[Context] = []

    async def asyncTearDown(self) -> None:
        for context in self.contexts:
            await unload_all(context)
        self._temp.cleanup()

    def _write_owners(self) -> None:
        registry = OwnerRegistry(self.data_dir / "owners.sqlite")
        registry.claim("ghost", "aetp_ghost_note")
        registry.claim("kept-owner", "aetp_kept_owner_note")
        registry.set_keep("kept-owner", True)

    async def _boot(self, policy: str) -> Context:
        self._write_owners()
        config_path = self.root / f"config-{policy}.ini"
        config_path.write_text(
            "[node]\n"
            "kind = master\n"
            f"install_root = {(self.root / 'installed').as_posix()}\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'aetp.db').as_posix()}\n"
            f"data_dir = {self.data_dir.as_posix()}\n"
            f"orphan_policy = {policy}\n",
            encoding="utf-8",
        )
        context = Context()
        context.provide("node_kind", "master")
        self.contexts.append(context)
        await context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await context.plugin(DbPlugin)
        return context

    async def test_warn_policy_keeps_orphans(self) -> None:
        await self._boot("warn")
        owners = OwnerRegistry(self.data_dir / "owners.sqlite")
        self.assertIn("ghost", owners.owners())  # 告警不删

    async def test_drop_policy_clears_orphans_but_keeps_kept(self) -> None:
        await self._boot("drop")
        owners = OwnerRegistry(self.data_dir / "owners.sqlite")
        self.assertNotIn("ghost", owners.owners())
        self.assertIn("kept-owner", owners.owners())  # keep_data 标记跳过


# ---------------------------------------------------------------- 插件生命周期 e2e
class PluginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """插件管理器全链路：安装 / 启用 / 卸载清库 / 被依赖保护（DESIGN §4.3、§4.5）。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.data_dir = self.root / "db-data"
        self.install_root = self.root / "installed"
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\n"
            "kind = master\n"
            f"install_root = {self.install_root.as_posix()}\n"
            "\n[web]\n"
            f"port = {_free_port()}\n"
            "auth_token = test-token\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'aetp.db').as_posix()}\n"
            f"data_dir = {self.data_dir.as_posix()}\n",
            encoding="utf-8",
        )
        self.context = Context()
        self.context.provide("node_kind", "master")
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)
        await self.context.plugin(WebApiPlugin)
        await self.context.plugin(PluginManagerPlugin)
        self.plugins = self.context.plugins

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self._temp.cleanup()

    def _producer_zip(self, *, upgraded: bool = False) -> bytes:
        source = _PRODUCER_SOURCE_V2 if upgraded else _PRODUCER_SOURCE_V1
        return _plugin_zip("data-producer", "数据生产", source)

    def _consumer_zip(self) -> bytes:
        source = (
            "class EntryPlugin:\n"
            "    name = 'data-consumer'\n"
            "    inject = ['db']\n"
            "    def __init__(self, ctx, config=None):\n"
            "        self.ctx = ctx\n"
            "    async def init(self):\n"
            "        self.ctx.logger.info('consumer ready')\n"
        )
        return _plugin_zip(
            "data-consumer", "数据消费", source, dependencies=_PRODUCER_DEPENDENCIES
        )

    async def _table_exists_async(self, table_name: str) -> bool:
        sqlite_master = table("sqlite_master", column("name"))
        rows = await self.context.get("db").execute(
            select(literal(True)).select_from(sqlite_master).where(sqlite_master.c.name == table_name)
        )
        return bool(rows)

    async def test_install_enable_uninstall_data_chain(self) -> None:
        # 装 B 提示 A 未安装（安装时依赖预检）
        with self.assertRaises(PluginManagerError) as missing:
            await self.plugins.install_archive(self._consumer_zip())
        self.assertIn("数据依赖未安装", str(missing.exception))

        await self.plugins.install_archive(self._producer_zip())
        await self.plugins.install_archive(self._consumer_zip())

        # A 表尚不存在 → 拒绝 B 加载，报明原因
        with self.assertRaises(PluginManagerError) as rejected:
            await self.plugins.enable("data-consumer")
        self.assertIn("不存在", str(rejected.exception))

        await self.plugins.enable("data-producer")
        self.assertTrue(await self._table_exists_async("aetp_data_producer_note"))
        await self.plugins.enable("data-consumer")

        # 被依赖时默认拒绝卸载
        with self.assertRaises(PluginManagerError) as refused:
            await self.plugins.uninstall("data-producer")
        self.assertIn("仍被依赖", str(refused.exception))

        # drop=True 强删 → 表清掉、卸载完成（安装记录一并移除）
        await self.plugins.uninstall("data-producer", drop=True)
        self.assertFalse(await self._table_exists_async("aetp_data_producer_note"))
        remaining = {item["id"] for item in await self.plugins.list_plugins()}
        self.assertNotIn("data-producer", remaining)
        self.assertIn("data-consumer", remaining)

    async def test_keep_data_preserves_tables(self) -> None:
        await self.plugins.install_archive(self._producer_zip())
        await self.plugins.enable("data-producer")
        self.assertTrue(await self._table_exists_async("aetp_data_producer_note"))

        await self.plugins.uninstall("data-producer", keep_data=True)
        self.assertTrue(await self._table_exists_async("aetp_data_producer_note"))
        owners = OwnerRegistry(self.data_dir / "owners.sqlite")
        self.assertTrue(owners.is_kept("data-producer"))

    async def _consumer_record(self) -> PublicPluginRecord:
        """取 data-consumer 的安装记录（含 enabled / last_error）。"""
        return next(
            item for item in await self.plugins.list_plugins() if item["id"] == "data-consumer"
        )

    async def test_schema_change_disables_consumer(self) -> None:
        await self.plugins.install_archive(self._producer_zip())
        await self.plugins.install_archive(self._consumer_zip())
        await self.plugins.enable("data-producer")
        await self.plugins.enable("data-consumer")

        # 生产方升级 V2（模型 + 迁移同批交付）：text 改名 body，requires 范围被破坏
        await self.plugins.install_archive(self._producer_zip(upgraded=True), replace=True)

        record = await self._consumer_record()
        deadline = time.monotonic() + 10
        while record["enabled"]:
            if time.monotonic() > deadline:
                self.fail("消费方未在超时内因数据依赖失效被停用")
            await asyncio.sleep(0.05)
            record = await self._consumer_record()
        self.assertIn("请更新本插件", record["last_error"] or "")


if __name__ == "__main__":
    unittest.main()
