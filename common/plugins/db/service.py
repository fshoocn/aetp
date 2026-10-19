"""数据库服务：``ctx.db``（``provide = "db"``）—— 仓储 / 会话 / 事务 / 结构同步。

业务代码只面向本模块的 ORM 抽象（模型 / 会话 / 查询表达式），换库 = 改
``config.ini`` 的 ``[db] url``，不改任何调用方代码（DESIGN §2）。

服务方法经代理调用时 ``self.ctx`` 是**调用方影子 ctx**（同 webapi 的
``_caller_fiber`` 模式）—— 表归属识别、跨插件访问强校验都由此来；直接在
服务实例上调用（db 自维护 / 测试）时无影子 ctx，按内部调用信任处理
（信任边界见 DESIGN §11-4）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

from cordis_port import Context, Fiber, Service
from sqlalchemy import ColumnElement, MetaData, Table, event, func
from sqlalchemy import delete as sa_delete
from sqlalchemy import insert as sa_insert
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select as sa_select
from sqlalchemy import text as sa_text
from sqlalchemy import update as sa_update
from sqlalchemy.engine import CursorResult, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import registry as sa_registry
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateColumn, CreateIndex

from .config import DbConfig
from .models import Model, decode_plugin_id, model_owner, owner_models, physical_name, registered_models, type_token
from .registry import (
    LOG_TABLE,
    DataDependency,
    OwnerRegistry,
    PluginRecordStore,
    consumers_of,
    find_orphans,
    fingerprint,
    meta_table,
    record_dependencies,
    schema_log_table,
)

#: 迁移步骤：``async def upgrade_v2(conn)``（SQLAlchemy Core / ``sa.text`` 均可）
MigrationStep = Callable[[AsyncConnection], Awaitable[None]]


class DbError(RuntimeError):
    """数据库服务层错误（仓储 / 结构同步的统一包装；逃生舱透传 SQLAlchemy 异常）。"""


class DbDependencyError(DbError):
    """跨插件数据依赖不满足（未声明 / 结构不符 / 依赖数据不存在）。"""


@dataclass
class SyncResult:
    """``sync_schema()`` 的执行摘要（测试与排障用）。"""

    #: 本次新建的物理表
    created: list[str] = field(default_factory=list[str])
    #: 加性对齐的变更摘要：物理表 → 变更列表
    altered: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    #: 执行到的迁移版本：owner → schema_version
    migrated: dict[str, int] = field(default_factory=dict[str, int])


T = TypeVar("T", bound=Model)

#: 当前环境会话（``session()`` / ``transaction()`` 作用域内，仓储操作自动汇入）
_CURRENT_SESSION: ContextVar[AsyncSession | None] = ContextVar("aetp_db_session", default=None)

#: 允许清理任意归属的平台管理面（卸载链路入口）；业务插件只能清理自己的归属
_CLEANUP_TRUSTED: frozenset[str] = frozenset({"plugin-manager"})


class Repo(Generic[T]):
    """泛型仓储：绑定模型与归属的日常 CRUD（单操作 = 单事务，DESIGN §3.2）。

    仓储层异常统一包装为 :class:`DbError`；查询表达式的全能力走
    :meth:`DbService.session`（逃生舱）。
    """

    def __init__(self, service: DbService, model: type[T], owner: str) -> None:
        self._service: DbService = service
        self._model: type[T] = model
        self._owner: str = owner

    @property
    def owner(self) -> str:
        """本仓储绑定的归属 owner。"""
        return self._owner

    @property
    def model(self) -> type[T]:
        """本仓储绑定的模型类。"""
        return self._model

    def _primary_key(self) -> ColumnElement[Any]:
        mapper = sa_inspect(self._model)
        keys = list(mapper.primary_key)
        if len(keys) != 1:
            raise DbError(f"仓储只支持单主键模型，{self._model.__name__} 有 {len(keys)} 个主键列")
        return keys[0]

    async def add(self, obj: T) -> T:
        """插入一条记录（返回同一实例）。"""
        try:
            async with self._service.session() as session:
                session.add(obj)
                await session.flush()
            return obj
        except SQLAlchemyError as exc:
            raise DbError(f"插入 {self._model.__name__} 失败：{exc}") from exc

    async def get(self, ident: object) -> T | None:
        """按主键查一条；不存在返回 ``None``。"""
        try:
            async with self._service.session() as session:
                return await session.get(self._model, ident)
        except SQLAlchemyError as exc:
            raise DbError(f"查询 {self._model.__name__}(id={ident!r}) 失败：{exc}") from exc

    async def list(
        self,
        *,
        where: ColumnElement[bool] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order_by: ColumnElement[Any] | Sequence[ColumnElement[Any]] | None = None,
    ) -> list[T]:
        """条件查询（分页 / 排序可选）。"""
        try:
            statement = sa_select(self._model)
            if where is not None:
                statement = statement.where(where)
            if order_by is not None:
                statement = statement.order_by(*([order_by] if not isinstance(order_by, Sequence) else order_by))
            if offset is not None:
                statement = statement.offset(offset)
            if limit is not None:
                statement = statement.limit(limit)
            async with self._service.session() as session:
                result = await session.execute(statement)
                return list(result.scalars().all())
        except SQLAlchemyError as exc:
            raise DbError(f"查询 {self._model.__name__} 列表失败：{exc}") from exc

    async def update(self, ident: object, values: Mapping[str, object]) -> int:
        """按主键改列，返回受影响行数。"""
        if not values:
            return 0
        try:
            statement = sa_update(self._model).where(self._primary_key() == ident).values(**dict(values))
            async with self._service.session() as session:
                result = await session.execute(statement)
                return int(cast(CursorResult[Any], result).rowcount or 0)
        except SQLAlchemyError as exc:
            raise DbError(f"更新 {self._model.__name__}(id={ident!r}) 失败：{exc}") from exc

    async def delete(self, ident: object) -> int:
        """按主键删一条，返回受影响行数。"""
        try:
            statement = sa_delete(self._model).where(self._primary_key() == ident)
            async with self._service.session() as session:
                result = await session.execute(statement)
                return int(cast(CursorResult[Any], result).rowcount or 0)
        except SQLAlchemyError as exc:
            raise DbError(f"删除 {self._model.__name__}(id={ident!r}) 失败：{exc}") from exc

    async def count(self, *, where: ColumnElement[bool] | None = None) -> int:
        """统计行数（可带条件）。"""
        try:
            statement = sa_select(func.count()).select_from(self._model)
            if where is not None:
                statement = statement.where(where)
            async with self._service.session() as session:
                result = await session.execute(statement)
                return int(result.scalar_one())
        except SQLAlchemyError as exc:
            raise DbError(f"统计 {self._model.__name__} 失败：{exc}") from exc


class DbService(Service[DbConfig]):
    """数据库服务载体（``ctx.db``）：引擎装配、归属识别与结构演进的统一入口。"""

    provide: str | None = "db"
    name: str = "db"
    Config: type[DbConfig] = DbConfig

    def __init__(
        self,
        ctx: Context,
        config: DbConfig | None = None,
        *,
        install_root: Path | None = None,
        base_dir: Path | None = None,
    ) -> None:
        # ⚠️ 必须最先调用：这一步完成 ctx.db 的注册
        super().__init__(ctx)
        self._provided_ctx: Context = ctx
        self.config: DbConfig = config if isinstance(config, DbConfig) else DbConfig()
        #: 节点目录（config.ini 所在目录）：相对库路径 / data_dir 的解析基准
        self._base_dir: Path | None = base_dir
        self._engine: AsyncEngine = self._build_engine()
        self._owners: OwnerRegistry = OwnerRegistry(self._data_dir() / "owners.sqlite")
        #: 插件安装记录（registry.db 事实源）；db 不反向注入 plugin-manager。
        #: DB-N3：事实源损坏/不可用 → 置 None，孤儿回收整体跳过（宁漏勿误删）
        self._registry_store: PluginRecordStore | None = None
        if install_root is not None:
            try:
                self._registry_store = PluginRecordStore(install_root / "registry.db")
            except (OSError, RuntimeError) as err:
                ctx.logger.warning("registry.db 不可用，本次会话跳过依赖事实源：%s", err)
        self._ddl_lock: asyncio.Lock = asyncio.Lock()
        self._external_cache: dict[tuple[str, str], type[object]] = {}
        self._started: bool = False

    # ------------------------------------------------------------- 生命周期
    def _anchored_url(self) -> str:
        """连接串：SQLite 相对库路径锚定到节点目录（与 ``install_root`` 等相对
        路径同约定——按 config.ini 所在目录解析，不随当前工作目录漂移）。"""
        url = self.config.resolved_url()
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite":
            return url
        database = parsed.database
        if not database or database == ":memory:":
            return url
        path = Path(database).expanduser()
        if not path.is_absolute() and self._base_dir is not None:
            path = self._base_dir / path
        return str(parsed.set(database=str(path)))

    def _data_dir(self) -> Path:
        """归属登记目录：相对路径锚定到节点目录。"""
        data_dir = self.config.resolve_data_dir()
        if not data_dir.is_absolute() and self._base_dir is not None:
            return self._base_dir / data_dir
        return data_dir

    def _build_engine(self) -> AsyncEngine:
        options: dict[str, Any] = {"echo": self.config.echo}
        try:
            url = self._anchored_url()
            parsed = make_url(url)
        except (SQLAlchemyError, ValueError) as exc:
            raise DbError(f"[db] url 不是合法的 SQLAlchemy 连接串：{exc}") from exc
        if parsed.get_backend_name() == "sqlite" and parsed.database in (None, "", ":memory:"):
            # 内存库单连接共享（否则每个连接一个独立库）
            options["poolclass"] = StaticPool
        elif parsed.get_backend_name() != "sqlite":
            options.update(
                pool_size=self.config.pool_size,
                max_overflow=self.config.max_overflow,
                pool_recycle=self.config.pool_recycle,
            )
        engine = create_async_engine(url, **options)
        if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
            _enable_sqlite_resilience(engine)
        return engine

    def _prepare_sqlite_file(self) -> None:
        """SQLite 文件库：父目录不存在则创建（默认 ``data/aetp.db`` 直接可用）。"""
        parsed = make_url(self._anchored_url())
        if parsed.get_backend_name() != "sqlite":
            return
        database = parsed.database
        if not database or database == ":memory:":
            return
        Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """启动自检：验证连接（快速暴露）、建库内元表（幂等）。

        抛出:
            DbError: 连接失败（url 非法 / 服务不通）——启动失败，错误进插件 ``last_error``。
        """
        warning = sqlite_pool_warning(self.config, self._engine.dialect.name)
        if warning is not None:
            self._provided_ctx.logger.warning(warning)
        try:
            self._prepare_sqlite_file()
            async with self._engine.begin() as conn:
                await conn.execute(sa_text("SELECT 1"))
                await conn.run_sync(_ensure_meta_tables)
        except (SQLAlchemyError, OSError, ValueError) as exc:
            # 异常原文只进服务端日志（连接错误可内嵌带账密的连接串）；
            # UI 可见的错误消息只带异常类型，不回显原文
            self._provided_ctx.logger.error("数据库启动失败：%s", exc)
            raise DbError(
                f"数据库连接失败（{self.config.to_dict().get('url')}）：{type(exc).__name__}"
            ) from exc
        self._started = True
        self._provided_ctx.logger.info(
            "数据库已就绪：%s（归属登记 %s）",
            self.config.to_dict().get("url"),
            self._owners.path,
        )

    async def shutdown(self) -> None:
        """排空并销毁连接池（热更新 / 停机专用：**不动任何表**，数据保留）。"""
        await self._engine.dispose()

    @property
    def engine(self) -> AsyncEngine:
        """逃生舱：SQLAlchemy 异步引擎（见 DESIGN §3.4）。"""
        return self._engine

    @property
    def dialect(self) -> str:
        """方言名（业务侧极少数需要分支时用，能不用则不用）。"""
        return self._engine.dialect.name

    # ---------------------------------------------------------- 归属识别
    def _caller_fiber(self) -> Fiber | None:
        """调用方 fiber；无影子 ctx（内部直调）或非插件上下文（根 fiber）返回 ``None``。

        归属识别只对插件 fiber 有意义（owner = 插件 id / fiber 名）；
        根 fiber（uid 0、无 ``runtime``）承载的是平台自身 / 测试直调，
        按内部调用信任处理（DESIGN §11-4）。
        """
        ctx = self.ctx
        if ctx is self._provided_ctx:
            return None
        fiber = getattr(ctx, "fiber", None)
        if fiber is None or fiber is self._provided_ctx.fiber:
            return None
        if getattr(fiber, "runtime", None) is None:
            return None
        return fiber

    def _caller_owner(self) -> str | None:
        """调用方归属：插件 id（合成模块解码）优先，否则 fiber 名（DESIGN §4.1）。"""
        fiber = self._caller_fiber()
        if fiber is None:
            return None
        callback = getattr(getattr(fiber, "runtime", None), "callback", None)
        decoded = decode_plugin_id(getattr(callback, "__module__", "") or "")
        if decoded:
            return decoded
        name = getattr(fiber, "name", None)
        return str(name) if name else f"fiber-{getattr(fiber, 'uid', '?')}"

    def _claim_owner(self, model: type[Model], owner: str | None) -> str:
        """解析模型归属并校验调用方权限：只有 owner 能动结构 / 写表。

        逃生舱：跨插件协作时显式传 ``owner=``（少用，视为显式授权）。
        """
        if getattr(model, "__external__", False):
            raise DbError("external_model 的反射映射只读，不接受仓储 / 结构操作")
        resolved = model_owner(model)
        if owner is not None and owner != resolved:
            raise DbError(f"owner={owner!r} 与模型物理归属 {resolved!r} 不符（物理表名已含归属前缀）")
        caller = self._caller_owner()
        if caller is not None and caller != resolved and owner is None:
            raise DbError(f"归属校验失败：表归 {resolved!r}，调用方是 {caller!r}；跨插件协作请显式 owner={resolved!r}")
        return resolved

    # ------------------------------------------------------------ 仓储 / 会话
    def repo(self, model: type[T], *, owner: str | None = None) -> Repo[T]:
        """取模型的泛型仓储（绑定归属；单操作 = 单事务）。"""
        resolved = self._claim_owner(model, owner)
        return Repo(self, model, resolved)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """独立会话（查询表达式全能力）：正常退出提交、异常自动回滚。

        在 ``session()`` / ``transaction()`` 作用域内调用时**复用当前会话**
        （仓储操作自动汇入环境事务，跨操作一致性由此保证）。
        """
        ambient = _CURRENT_SESSION.get()
        if ambient is not None:
            yield ambient
            return
        async with AsyncSession(self._engine, expire_on_commit=False) as session:
            token = _CURRENT_SESSION.set(session)
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
            finally:
                _CURRENT_SESSION.reset(token)

    @asynccontextmanager
    async def transaction(self, session: AsyncSession | None = None) -> AsyncGenerator[AsyncSession, None]:
        """显式事务：异常自动回滚；嵌套（含已有环境事务）即 SAVEPOINT（DESIGN §3.3）。"""
        if session is not None:
            async with session.begin_nested():
                yield session
            return
        ambient = _CURRENT_SESSION.get()
        if ambient is not None:
            async with ambient.begin_nested():
                yield ambient
            return
        try:
            async with AsyncSession(self._engine, expire_on_commit=False) as fresh:
                token = _CURRENT_SESSION.set(fresh)
                try:
                    async with fresh.begin():
                        yield fresh
                finally:
                    _CURRENT_SESSION.reset(token)
        except SQLAlchemyError as exc:
            raise DbError(f"事务执行失败：{exc}") from exc

    async def execute(self, statement: Any) -> list[Any]:
        """逃生舱：SQLAlchemy Core 原语（返回已物化的行列表）。

        在 ``session()`` / ``transaction()`` 作用域内调用时**汇入环境事务**
        （DB-N4）：不再自开自动提交连接，外层失败时本调用的写入随外层回滚，
        「失败整体回滚」的承诺不被破坏；无环境事务时自动提交执行。
        """
        ambient = _CURRENT_SESSION.get()
        if ambient is not None:
            result = cast("CursorResult[Any]", await ambient.execute(statement))
            return list(result.fetchall()) if result.returns_rows else []
        async with self._engine.begin() as conn:
            result = await conn.execute(statement)
            return list(result.fetchall()) if result.returns_rows else []

    # ---------------------------------------------------------- 结构同步
    async def sync_schema(
        self,
        *,
        models: Sequence[type[Model]] | None = None,
        migrations: Sequence[tuple[int, MigrationStep]] | None = None,
    ) -> SyncResult:
        """建缺表 → 加性对齐（L1）→ 执行未跑的迁移步骤（L2），幂等。

        只管调用方认领的表（显式 ``models`` 时按各模型归属校验）；DDL 全程
        串行（进程内锁，DESIGN §11-5）。结构变化自动更新指纹、写变更日志并
        反查消费方告警（锁步模型：不阻断，DESIGN §4.5）。
        """
        if models is not None:
            targets = list(models)
        else:
            caller = self._caller_owner()
            targets = owner_models(caller) if caller is not None else registered_models()
        owners: set[str] = {self._claim_owner(model, None) for model in targets}

        result = SyncResult()
        async with self._ddl_lock:
            async with self._engine.begin() as conn:
                await conn.run_sync(_ensure_meta_tables)
            # 阶段 1（L0 / L1 前半）：登记 → 建缺表 → 加性对齐（宽松：
            # 不可加列留给 L2 迁移处理，迁移后严格补漏再报错）
            summaries: dict[str, list[str]] = {}
            for model in targets:
                owner = self._claim_owner(model, None)
                table = _model_table(model)
                # 登记先于建表：崩溃后孤儿可回收（DESIGN §4.2）
                self._owners.claim(owner, table.name)
                if await self._ensure_table(table):
                    result.created.append(table.name)
                    summaries[table.name] = ["create table"]
                else:
                    changes = await self._align_table(table, strict=False)
                    if changes:
                        result.altered[table.name] = list(changes)
                        summaries[table.name] = list(changes)

            # 阶段 2（L2）：版本化迁移步骤，按序续跑；本次新建的表即基线
            if migrations:
                self._check_migration_scope(owners, migrations)
                owner = next(iter(owners))
                fresh = all(
                    _model_table(model).name in result.created
                    for model in targets
                    if model_owner(model) == owner
                )
                migrated_to = await self._run_migrations(owner, migrations, fresh=fresh)
                if migrated_to is not None:
                    result.migrated[owner] = migrated_to

            # 阶段 2b（L1 后半）：迁移改过结构后严格补漏；单表失败**局部化**
            # （DB-N1）：记录后继续其他表，不让阶段 3 的指纹/日志/消费方告警
            # 被连累跳过；全部跑完后聚合报错，保证问题不消音
            strict_failures: list[str] = []
            for model in targets:
                table = _model_table(model)
                if table.name in result.created:
                    continue
                try:
                    changes = await self._align_table(table, strict=True)
                except DbError as exc:
                    strict_failures.append(str(exc))
                    continue
                if changes:
                    result.altered.setdefault(table.name, []).extend(changes)
                    summaries.setdefault(table.name, []).extend(changes)

            # 阶段 3：指纹（迁移后再算，覆盖 L2 的结构变化）+ 变更日志 + 消费方告警
            for model in targets:
                owner = self._claim_owner(model, None)
                await self._sync_fingerprint(owner, _model_table(model), summaries.get(_model_table(model).name, ()))
            await self._notice_consumers(owners, result)
        if strict_failures:
            raise DbError("结构对齐未全部完成（其余表已记账）：" + "；".join(strict_failures))
        return result

    def _check_migration_scope(self, owners: set[str], migrations: Sequence[tuple[int, MigrationStep]]) -> None:
        if len(owners) != 1:
            raise DbError(f"迁移步骤（MIGRATIONS）只能归属单个 owner，当前模型横跨 {sorted(owners)}")
        seen: set[int] = set()
        for version, _ in migrations:
            if version < 2:
                raise DbError(f"迁移版本号需为 ≥ 2 的整数（1 保留给建表基线），收到 {version!r}")
            if version in seen:
                raise DbError(f"迁移版本号重复：{version}")
            seen.add(version)        # 连续性（DB-N7）：版本号需从 2 起无缝衔接，否则升级路径会跳过缺号步骤
        expected = list(range(2, max(seen) + 1))
        if sorted(seen) != expected:
            missing = sorted(set(expected) - seen)
            raise DbError(f"迁移版本号不连续，缺 {missing}（步骤需从 2 起逐版本衔接）")
    async def _ensure_table(self, table: Table[Any]) -> bool:
        """建缺表（幂等），返回是否新建。"""

        def work(sync_conn: Any) -> bool:
            inspector = sa_inspect(sync_conn)
            if inspector.has_table(table.name):
                return False
            table.metadata.create_all(sync_conn, tables=[table], checkfirst=True)
            return True

        async with self._engine.begin() as conn:
            return bool(await conn.run_sync(work))

    async def _align_table(self, table: Table[Any], *, strict: bool = True) -> list[str]:
        """L1 加性对齐：缺列 ``ADD COLUMN``、缺索引补建、放宽长度（零代码）。

        ``strict=False``（迁移前宽松趟）：不可加列（非空且无服务端默认值）
        先跳过——可能由紧随其后的 L2 迁移（改名 / 拆表）消化；
        ``strict=True``（迁移后补漏趟）：残留的不可加列报错，提示写迁移步骤。
        """

        def work(sync_conn: Any) -> list[str]:
            inspector = sa_inspect(sync_conn)
            existing = {column["name"]: column for column in inspector.get_columns(table.name)}
            changes: list[str] = []
            preparer = sync_conn.dialect.identifier_preparer
            quote_table = preparer.quote(table.name)
            for column in table.c:
                if column.name in existing:
                    continue
                if not column.nullable and column.server_default is None:
                    if strict:
                        raise DbError(
                            f"表 {table.name} 缺列 {column.name}（非空且无服务端默认值），"
                            f"无法自动加列；请在插件的 MIGRATIONS 里写迁移步骤"
                        )
                    continue
                fragment = str(CreateColumn(column).compile(dialect=sync_conn.dialect))
                sync_conn.execute(sa_text(f"ALTER TABLE {quote_table} ADD COLUMN {fragment}"))
                changes.append(f"add column {column.name}: {type_token(column.type)}")

            existing_indexes = {index["name"] for index in inspector.get_indexes(table.name)}
            for index in table.indexes:
                if (index.name or "") not in existing_indexes:
                    sync_conn.execute(CreateIndex(index))
                    changes.append(f"add index {index.name}")

            if sync_conn.dialect.name in {"postgresql", "mysql", "mariadb"}:
                for column in table.c:
                    current = existing.get(column.name)
                    current_type = current.get("type") if current else None
                    if wider_than(column.type, current_type):
                        type_sql = column.type.compile(dialect=sync_conn.dialect)
                        quote_column = preparer.quote(column.name)
                        if sync_conn.dialect.name == "postgresql":
                            sync_conn.execute(
                                sa_text(f"ALTER TABLE {quote_table} ALTER COLUMN {quote_column} TYPE {type_sql}")
                            )
                        else:
                            nullable_sql = "" if column.nullable else " NOT NULL"
                            sync_conn.execute(
                                sa_text(f"ALTER TABLE {quote_table} MODIFY COLUMN {quote_column} {type_sql}{nullable_sql}")
                            )
                        changes.append(f"widen column {column.name}")
            return changes

        async with self._engine.begin() as conn:
            return list(await conn.run_sync(work))

    async def _run_migrations(
        self, owner: str, migrations: Sequence[tuple[int, MigrationStep]], *, fresh: bool
    ) -> int | None:
        """L2 迁移：按版本续跑，步骤与版本记账同事务（失败回滚不前移）。

        ``fresh=True``（本次 L0 刚建的表）：表结构已是当前版本，历史迁移
        不重跑，直接记最新版本为基线（否则新装插件会被旧步骤误伤）。
        """
        current = await self._meta_value(owner, "schema_version")
        latest = max(version for version, _ in migrations)
        if fresh and not (current and current.isdigit()):
            async with self._engine.begin() as conn:
                await self._meta_put(conn, owner, "schema_version", str(latest))
                await self._append_log(conn, owner, "", "", "baseline", f"fresh schema at v{latest}")
            return latest
        version_now = int(current) if current and current.isdigit() else 1
        reached: int | None = None
        for version, step in sorted(migrations, key=lambda item: item[0]):
            if version <= version_now:
                continue
            try:
                async with self._engine.begin() as conn:
                    await step(conn)
                    await self._meta_put(conn, owner, "schema_version", str(version))
                    await self._append_log(conn, owner, "", "", "migrate", f"upgrade to v{version}")
            except SQLAlchemyError as exc:
                raise DbError(f"迁移步骤 v{version}（owner={owner}）失败已回滚，版本不前移：{exc}") from exc
            version_now = version
            reached = version
        return reached

    async def _sync_fingerprint(
        self, owner: str, table: Table[Any], summary: Sequence[str] = ()
    ) -> None:
        """更新结构指纹（范围 = 整表）；结构变化即写变更日志。"""
        columns = [(column.name, type_token(column.type)) for column in table.c]
        current = fingerprint(columns)
        key = f"fp:{table.name}"
        async with self._engine.begin() as conn:
            previous = await self._meta_value_on(conn, owner, key)
            await self._meta_put(conn, owner, key, current)
            if summary:
                await self._append_log(conn, owner, "", table.name, "alter", "; ".join(summary))
            elif previous is not None and previous != current:
                await self._append_log(
                    conn, owner, "", table.name, "alter", f"fingerprint {previous} -> {current}"
                )

    async def _notice_consumers(self, owners: set[str], result: SyncResult) -> None:
        """结构变化后反查消费方：锁步模型只告警，不阻断（DESIGN §4.5）。"""
        changed = [name for name in result.altered] + [name for name in result.migrated]
        if not changed:
            return
        self._external_cache.clear()  # 结构已变，反射映射缓存作废
        records = self._installed_records()
        for owner in sorted(owners):
            consumers = consumers_of(records, owner)
            if consumers:
                self._provided_ctx.logger.warning(
                    "owner %s 的结构已变化（%s），以下消费方需要更新：%s",
                    owner,
                    ", ".join(sorted(changed)),
                    ", ".join(sorted(consumers)),
                )
            self._emit_schema_changed(
                {"owner": owner, "summary": sorted(changed), "consumers": sorted(consumers)}
            )

    def _emit_schema_changed(self, payload: dict[str, object]) -> None:
        """派发 ``db/schema-changed``（派发窗口隔离环境会话，DB-H3）。

        监听器通常会 ``create_task`` 派生后台任务（如消费方复检）；任务会
        **复制当前 ContextVar**——派发时不隔离就会把外层事务的
        ``_CURRENT_SESSION`` 带进子任务，与 SQLAlchemy「会话不可跨任务
        共用」冲突。这里在派发窗口内显式清空，监听器及其派生任务看到的
        环境会话都是 ``None``。
        """
        token = _CURRENT_SESSION.set(None)
        try:
            self._provided_ctx.emit("db/schema-changed", payload)
        finally:
            _CURRENT_SESSION.reset(token)

    # ---------------------------------------------------------- 清理与孤儿回收
    def _require_cleanup_rights(self, owner: str | None = None) -> None:
        """破坏性清理的调用方校验（DB-H1，同款机制于 :meth:`_claim_owner`）。

        * 内部直调（平台自身 / 测试，``_caller_fiber`` 为 ``None``）信任；
        * 业务插件只能清理**自己的**归属（``caller == owner``）；
        * 平台管理面（``plugin-manager``，卸载链路的唯一入口）可清理任意归属。
        """
        caller = self._caller_owner()
        if caller is None or caller in _CLEANUP_TRUSTED:
            return
        if owner is not None and caller == owner:
            return
        target = repr(owner) if owner else "全部归属"
        raise DbError(
            f"越权：调用方 {caller!r} 不能清理 {target} 的数据"
            "（业务插件只能清理自己的归属）"
        )

    async def drop_owner(
        self, owner: str, *, keep_data: bool = False, force: bool = False
    ) -> list[str]:
        """按归属清理数据表（卸载链路调用），返回**删除失败的残表**清单。

        * 仍有消费方登记时默认**拒绝**（``force=True`` 强删，DESIGN §4.5）；
        * ``keep_data=True`` 只标记保留、不动表（孤儿回收跳过）；
        * 单表 drop 失败重试一次，仍失败则**登记保留**并记错误日志（DESIGN
          §4.3），但已成功表的登记 / 指纹即时清理、不再整体悬停（DB-N2）；
          残表可观测（返回值 + 日志）、可重试，重新安装即重新认领自愈。
        """
        self._require_cleanup_rights(owner)
        async with self._ddl_lock:
            consumers = consumers_of(self._installed_records(), owner)
            if consumers and not force:
                raise DbDependencyError(
                    f"{owner!r} 的数据仍被依赖：{', '.join(sorted(consumers))}；"
                    f"请先处理依赖方，或用 drop=True 强制删除"
                )
            if keep_data:
                self._owners.set_keep(owner, True)
                self._provided_ctx.logger.info("owner %s 的数据按 keep_data 保留", owner)
                return []

            failed: list[str] = []
            for table_name in self._owners.tables(owner):
                dropped = await self._drop_table(table_name)
                if dropped:
                    self._owners.drop_table(owner, table_name)
                    await self._clear_table_meta(owner, table_name)
                else:
                    failed.append(table_name)
            if failed:
                self._provided_ctx.logger.error(
                    "owner %s 的表清理失败（登记保留，待孤儿回收 / 重试）：%s",
                    owner,
                    ", ".join(failed),
                )
                return failed
            self._owners.release(owner)
            self._external_cache.clear()
            await self._clear_owner_meta(owner)
            self._provided_ctx.logger.info("owner %s 的数据表已清理", owner)
            return []

    async def _clear_table_meta(self, owner: str, table_name: str) -> None:
        """清理单表的指纹记账（部分失败时已成功表不悬停，DB-N2）。"""
        async with self._engine.begin() as conn:
            await conn.execute(
                sa_delete(meta_table).where(
                    meta_table.c.owner == owner,
                    meta_table.c.name == f"fp:{table_name}",
                )
            )

    async def _drop_table(self, table_name: str) -> bool:
        """单表 drop（幂等）；失败重试一次。"""
        preparer = self._engine.dialect.identifier_preparer
        statement = sa_text(f"DROP TABLE IF EXISTS {preparer.quote(table_name)}")
        for _attempt in (1, 2):
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(statement)
                return True
            except SQLAlchemyError as exc:
                last_error = exc
        self._provided_ctx.logger.error("drop 表 %s 失败：%s", table_name, last_error)
        return False

    async def sweep_orphans(self, installed: Iterable[str], policy: str) -> list[str]:
        """启动时孤儿回收：登记在、插件已不存在的 owner 按 ``orphan_policy`` 处理。"""
        self._require_cleanup_rights()
        orphans = [owner for owner in find_orphans(self._owners.owners(), installed) if not self._owners.is_kept(owner)]
        for owner in orphans:
            if policy == "drop":
                await self.drop_owner(owner, force=True)
                self._provided_ctx.logger.warning("孤儿归属 %s 已按 orphan_policy=drop 清理", owner)
            else:
                self._provided_ctx.logger.warning(
                    "孤儿归属 %s（表：%s）的插件已不存在；orphan_policy=warn 不清理",
                    owner,
                    ", ".join(self._owners.tables(owner)),
                )
        return orphans

    # ---------------------------------------------------------- 跨插件依赖
    async def check_dependencies(self, plugin_id: str, dependencies: Sequence[DataDependency]) -> None:
        """消费方加载前校验数据依赖（锁步模型）：表不存在 / ``requires`` 不符即拒载。

        抛出:
            DbDependencyError: 报明原因（「依赖不存在」或「A 的结构已变化，请更新本插件」）。
        """
        for dependency in dependencies:
            owner, data, requires = dependency["plugin"], dependency["data"], dependency["requires"]
            table = await self._reflect_quiet(owner, data)
            if table is None:
                raise DbDependencyError(
                    f"插件 {plugin_id!r} 的数据依赖不满足：依赖 {owner}/{data} 不存在，请先安装并启用 {owner!r}"
                )
            problems = _compare_requires(table, requires)
            if problems:
                raise DbDependencyError(
                    f"插件 {plugin_id!r} 的数据依赖不满足：{owner} 的结构已变化"
                    f"（{data}：{'; '.join(problems)}；diff 见 {LOG_TABLE} 变更日志），请更新本插件"
                )

    async def recheck_consumers(self, owner: str) -> list[tuple[str, str]]:
        """复检某 owner 的全部消费方（``db/schema-changed`` 后调用），返回失配清单。"""
        failures: list[tuple[str, str]] = []
        for consumer, dependencies in consumers_of(self._installed_records(), owner).items():
            try:
                await self.check_dependencies(consumer, dependencies)
            except DbDependencyError as exc:
                failures.append((consumer, str(exc)))
        return failures

    async def wait_for_data(self, owner: str, data: str, timeout: float = 30.0) -> bool:
        """逃生舱：等待某张数据表出现（乱序启动容忍）；超时返回 ``False``。"""
        deadline = time.monotonic() + timeout
        while True:
            table = await self._reflect_quiet(owner, data)
            if table is not None:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def external_model(self, owner: str, data: str) -> type[object]:
        """跨插件只读联查的反射映射（模式 c，DESIGN §4.5）。

        需在调用方 ``plugin.json`` 的 ``dependencies`` 显式声明（运行时强校验，
        未声明一律拒绝）；表无主键时无法映射 ORM 模型，请用 :meth:`external_table`。
        """
        self._require_declaration(owner, data)
        cached = self._external_cache.get((owner, data))
        if cached is not None:
            return cached
        table = await self._reflect(owner, data)
        if not list(table.primary_key.columns):
            raise DbError(f"依赖表 {owner}/{data} 没有主键，无法映射为只读模型；请用 external_table() 联查")
        class_name = f"External_{owner}_{data}".replace("-", "_")
        mapped: type[object] = type(class_name, (), {"__module__": __name__, "__external__": True})
        sa_registry().map_imperatively(mapped, table)  # 返回 Mapper，映射挂在类上
        self._external_cache[(owner, data)] = mapped
        return mapped

    async def external_table(self, owner: str, data: str) -> Table[Any]:
        """跨插件只读联查的反射表（``.c`` 列访问；无主键表的联查逃生舱）。"""
        self._require_declaration(owner, data)
        return await self._reflect(owner, data)

    def _require_declaration(self, owner: str, data: str) -> None:
        """跨 owner 访问强校验：调用方必须在 ``dependencies`` 声明（DESIGN §4.5②）。"""
        caller = self._caller_owner()
        if caller is None:
            return  # 内部直调（db 自维护 / 测试）：信任边界见 DESIGN §11-4
        for dependency in record_dependencies(self._installed_records().get(caller)):
            if dependency["plugin"] == owner and dependency["data"] == data:
                return
        raise DbDependencyError(
            f"{caller!r} 未在 plugin.json 的 dependencies 声明 {owner!r}/{data!r} 的数据依赖，拒绝访问"
        )

    async def _reflect(self, owner: str, data: str) -> Table[Any]:
        table = await self._reflect_quiet(owner, data)
        if table is None:
            raise DbDependencyError(f"依赖 {owner}/{data} 不存在（表 {physical_name(owner, data)}）")
        return table

    async def _reflect_quiet(self, owner: str, data: str) -> Table[Any] | None:
        """按现库结构反射只读表；不存在返回 ``None``。"""
        target = physical_name(owner, data)
        metadata = MetaData()

        def work(sync_conn: Any) -> Table[Any] | None:
            inspector = sa_inspect(sync_conn)
            if not inspector.has_table(target):
                return None
            return Table(target, metadata, autoload_with=sync_conn)

        try:
            async with self._engine.connect() as conn:
                return await conn.run_sync(work)
        except SQLAlchemyError as exc:
            raise DbError(f"反射表 {target} 失败：{exc}") from exc

    # ---------------------------------------------------------- 库内元数据
    async def _meta_value(self, owner: str, name: str) -> str | None:
        async with self._engine.connect() as conn:
            return await self._meta_value_on(conn, owner, name)

    async def _meta_value_on(self, conn: AsyncConnection, owner: str, name: str) -> str | None:
        statement = sa_select(meta_table.c.value).where(meta_table.c.owner == owner, meta_table.c.name == name)
        row = (await conn.execute(statement)).first()
        return str(row[0]) if row is not None else None

    async def _meta_put(self, conn: AsyncConnection, owner: str, name: str, value: str) -> None:
        await conn.execute(sa_delete(meta_table).where(meta_table.c.owner == owner, meta_table.c.name == name))
        await conn.execute(
            sa_insert(meta_table).values(owner=owner, name=name, value=value, updated_at=time.time())
        )

    async def _clear_owner_meta(self, owner: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(sa_delete(meta_table).where(meta_table.c.owner == owner))
            await conn.execute(sa_delete(schema_log_table).where(schema_log_table.c.owner == owner))

    async def _append_log(
        self,
        conn: AsyncConnection,
        owner: str,
        plugin_version: str,
        table_name: str,
        action: str,
        summary: str,
    ) -> None:
        """写变更日志（按 owner 各自上限内裁剪，防无限增长）。"""
        await conn.execute(
            sa_insert(schema_log_table).values(
                ts=time.time(),
                owner=owner,
                plugin_version=plugin_version,
                table_name=table_name,
                action=action,
                summary=summary[:2048],
            )
        )
        limit = self.config.schema_log_limit
        # 按 owner 各保最新 limit 条（DB-N9）：日志多的 owner 不再挤掉别人的
        owner_rows = await conn.execute(sa_select(schema_log_table.c.owner).distinct())
        for (owner_name,) in owner_rows.all():
            keep = [
                row[0]
                for row in (
                    await conn.execute(
                        sa_select(schema_log_table.c.id)
                        .where(schema_log_table.c.owner == owner_name)
                        .order_by(schema_log_table.c.id.desc())
                        .limit(limit)
                    )
                ).all()
            ]
            await conn.execute(
                sa_delete(schema_log_table).where(
                    schema_log_table.c.owner == owner_name,
                    schema_log_table.c.id.not_in(keep),
                )
            )

    # ---------------------------------------------------------- 插件事实源
    def _installed_records(self) -> dict[str, object]:
        """读插件安装记录（``registry.db``，只读事实源）。"""
        if self._registry_store is None:
            return {}
        try:
            return self._registry_store.read()
        except (OSError, RuntimeError) as exc:
            self._provided_ctx.logger.error("插件安装记录无法读取：%s", exc)
            return {}


def _enable_sqlite_resilience(engine: AsyncEngine) -> None:
    """SQLite 文件库的并发韧性（DB-N5）：WAL + ``busy_timeout``。

    高并发 DDL / 写入时不再轻易 ``database is locked``（WAL 读写不互斥，
    忙等 5 秒兕底）。
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


def sqlite_pool_warning(config: DbConfig, dialect_name: str) -> str | None:
    """DB-N6：SQLite 方言不用连接池参数，显式配置了就告警（不静默无效）。"""
    if dialect_name != "sqlite":
        return None
    if (
        config.pool_size != DbConfig.pool_size
        or config.max_overflow != DbConfig.max_overflow
    ):
        return (
            "SQLite 方言不使用连接池参数，pool_size / max_overflow 已忽略"
            f"（配置值 {config.pool_size} / {config.max_overflow}）"
        )
    return None


def _ensure_meta_tables(sync_conn: Any) -> None:
    """库内元表（``aetp_db_meta`` / ``aetp_schema_log``）幂等建表。"""
    meta_table.metadata.create_all(sync_conn, checkfirst=True)


def _model_table(model: type[Model]) -> Table[Any]:
    table = getattr(model, "__table__", None)
    if not isinstance(table, Table):
        raise DbError(f"模型 {model.__name__} 没有映射数据表（抽象基类不能直接同步）")
    return cast(Table[Any], table)


def _compare_requires(table: Table[Any], requires: Mapping[str, str]) -> list[str]:
    """``requires`` 范围校验：列在 + 类型符一致，返回问题清单（空 = 通过）。"""
    existing = {column.name: type_token(column.type) for column in table.c}
    problems: list[str] = []
    for name, token in requires.items():
        if name not in existing:
            problems.append(f"缺列 {name}")
        elif existing[name] != token:
            problems.append(f"列 {name} 类型 {existing[name]} ≠ 声明 {token}")
    return problems


def wider_than(current: object, previous: object) -> bool:
    """放宽判定（DB-N8）：字符串长度放宽，或整数族位宽放宽
    （``SmallInteger → Integer → BigInteger``）；其余类型交给 L2 迁移。"""
    current_length = getattr(current, "length", None)
    previous_length = getattr(previous, "length", None)
    if isinstance(current_length, int) and isinstance(previous_length, int):
        return current_length > previous_length
    current_width = _INTEGER_WIDTH.get(type(current).__name__)
    previous_width = _INTEGER_WIDTH.get(type(previous).__name__)
    return (
        current_width is not None
        and previous_width is not None
        and current_width > previous_width
    )


#: 整数族位宽（DB-N8）：同族仅当位宽变大才算 L1 可自动放宽
_INTEGER_WIDTH: dict[str, int] = {
    "SmallInteger": 16,
    "Integer": 32,
    "BigInteger": 64,
}


__all__: list[str] = [
    "DbDependencyError",
    "DbError",
    "DbService",
    "MigrationStep",
    "Repo",
    "SyncResult",
    "sqlite_pool_warning",
    "wider_than",
]
