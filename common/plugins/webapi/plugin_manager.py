"""本地可信插件包的安装与生命周期管理。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import types
import zipfile
from collections.abc import Iterable, Mapping
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Final, TypedDict, cast

from cordis_port import Context, Fiber
from sqlalchemy.exc import SQLAlchemyError

from common.cordis_utils import safe_dispose
from common.plugins.db.registry import DataDependency, parse_dependencies
from common.plugins.db.service import DbDependencyError, DbError

MAX_ARCHIVE_SIZE: Final[int] = 32 * 1024 * 1024
MAX_EXTRACTED_SIZE: Final[int] = 128 * 1024 * 1024
MAX_ARCHIVE_FILES: Final[int] = 2048
MAX_REQUIRES: Final[int] = 32
PLUGIN_ID: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ENTRYPOINT: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):([A-Za-z_][A-Za-z0-9_]*)$")
#: 插件目标节点类型（``plugin.json`` 的 ``kind``，**必填**，单值或列表）：
#: ``master`` / ``slave`` / ``any``（不限）。这是插件的归属与安装准入（取值与
#: :class:`common.node_runtime.NodeKind` 一致）；列表 = 同时支持多种节点，
#: 如 ``["master", "slave"]``（主从通用）。安装前校验覆盖当前节点；
#: ``kind = slave`` 的插件也是未来分发到从节点的候选。
PLUGIN_KINDS: Final[tuple[str, ...]] = ("master", "slave", "any")
#: 安装/归档时跳过的目录（构建缓存、依赖与前端源码——交付包只带运行时所需，
#: 前端构建产物在 ``static/``；打包与 digest 比对共用本清单，口径一致）
EXCLUDED_DIRS: Final[frozenset[str]] = frozenset(
    {"node_modules", "__pycache__", ".git", ".venv", "venv", "frontend"}
)
#: 安装/归档时跳过的文件后缀
EXCLUDED_SUFFIXES: Final[tuple[str, ...]] = (".pyc", ".pyo")
#: 插件描述文件（约定名，位于插件包根目录；Markdown 格式，讲述插件功能、
#: 能力接口与使用方法）
PLUGIN_README: Final[str] = "README.md"
#: 描述文件读取上限（插件文档可能含图片链接/长接口列表，放宽到 10 MiB）
MAX_README_BYTES: Final[int] = 10 * 1024 * 1024

#: ``common/plugins/`` 目录（内置插件的包根都在这里）
_PLUGINS_ROOT: Final[Path] = Path(__file__).resolve().parents[1]


class _BuiltinSpec(TypedDict):
    """内置插件的展示元数据（``root`` 是包根，描述文件 README.md 所在）。"""

    name: str
    version: str
    entrypoint: str
    kind: list[str]
    root: Path


#: 内置插件（``common/plugins/`` 直装、由 node_runtime 装配，不走安装器）：
#: 列表照常展示，但**禁止安装 / 升级 / 启停 / 卸载**——上传同 id 插件包
#: 一律拒绝，否则会装出与内置服务同名冲突的「影子插件」。
#:
#: 内置身份**自动识别**（不维护手写清单）：凡从 ``common.plugins`` 包树
#: 加载的插件类即内置，展示元数据取插件类属性，见 :func:`builtin_specs`。


def is_builtin_plugin(plugin: object) -> bool:
    """插件类是否内置：定义在 ``common.plugins`` 包树下即内置。"""
    module = getattr(plugin, "__module__", "") or ""
    return module == "common.plugins" or module.startswith("common.plugins.")


def _builtin_entrypoint(plugin: type) -> str:
    """插件类的规范导入路径：包 ``__init__`` 重导出时取包路径，否则取定义模块。"""
    module = plugin.__module__
    package = module.rpartition(".")[0]
    if package and getattr(sys.modules.get(package), plugin.__name__, None) is plugin:
        module = package
    return f"{module}:{plugin.__name__}"


def builtin_specs(ctx: Context) -> dict[str, _BuiltinSpec]:
    """当前上下文**已加载**的内置插件元数据（key 为插件 id）。

    事实源是 cordis 注册表里已加载的插件：凡从 ``common.plugins`` 包树加载的
    插件类默认即内置（安装插件是 ``_aetp_plugin_*`` 合成模块名，天然不命中）。
    展示名 / 版本取插件类属性 ``display_name`` / ``version``（缺省回退
    id / ``1.0.0``），``entrypoint`` 取规范导入路径，``root`` 为类定义所在
    目录（描述文件 README.md 所在）。
    """
    specs: dict[str, _BuiltinSpec] = {}
    for runtime in list(ctx.registry.values()):
        plugin = runtime.callback
        if not isinstance(plugin, type) or not is_builtin_plugin(plugin):
            continue
        plugin_id = getattr(plugin, "name", None) or runtime.name
        if not isinstance(plugin_id, str) or not plugin_id:
            continue
        module_file = getattr(sys.modules.get(plugin.__module__), "__file__", None)
        specs[plugin_id] = {
            "name": getattr(plugin, "display_name", None) or plugin_id,
            "version": getattr(plugin, "version", None) or "1.0.0",
            "entrypoint": _builtin_entrypoint(plugin),
            "kind": list(getattr(plugin, "kind", None) or ["any"]),
            "root": Path(module_file).parent if module_file else _PLUGINS_ROOT,
        }
    return specs




def _is_excluded_entry(name: str) -> bool:
    """包内容中跳过的条目：构建缓存目录、依赖目录与旧字节码（不进包、不解包）。"""
    path = PurePosixPath(name.replace("\\", "/"))
    return any(part in EXCLUDED_DIRS for part in path.parts) or path.suffix.lower() in EXCLUDED_SUFFIXES


def _content_digest(entries: list[tuple[str, bytes]]) -> str:
    """包内容摘要：相对路径 + 文件内容的稳定 sha256（与打包时间戳无关）。"""
    hasher = hashlib.sha256()
    for name, data in sorted(entries):
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(data).digest())
    return hasher.hexdigest()


def package_digest(root: Path) -> str:
    """插件源/安装目录的内容摘要（跳过构建缓存、旧字节码与符号链接）。"""
    entries: list[tuple[str, bytes]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in EXCLUDED_DIRS]
        for filename in sorted(filenames):
            path = Path(dirpath, filename)
            if path.suffix.lower() in EXCLUDED_SUFFIXES or path.is_symlink():
                continue
            entries.append((path.relative_to(root).as_posix(), path.read_bytes()))
    return _content_digest(entries)


def archive_digest(archive: bytes) -> str:
    """zip 交付包的内容摘要（口径与 :func:`package_digest` 一致）。"""
    entries: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(BytesIO(archive)) as package:
        for member in package.infolist():
            if member.is_dir() or _is_excluded_entry(member.filename):
                continue
            name = PurePosixPath(member.filename.replace("\\", "/")).as_posix()
            entries.append((name, package.read(member)))
    return _content_digest(entries)


def source_digest(source: str | Path) -> str:
    """插件源（源目录或 zip 交付包）的内容摘要。"""
    path = Path(source)
    if path.suffix.lower() == ".zip":
        return archive_digest(path.read_bytes())
    return package_digest(path)


def source_plugin_id(source: str | Path) -> str:
    """从插件源（源目录或 zip 交付包）的 ``plugin.json`` 读取插件 id。"""
    source = Path(source)
    manifest: object
    if source.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(source) as package:
                raw = package.read("plugin.json")
            manifest = json.loads(raw)
        except (OSError, KeyError, ValueError, zipfile.BadZipFile, RecursionError) as exc:
            raise ValueError(f"插件包无法读取 plugin.json：{source}：{exc}") from exc
    else:
        manifest_path = source / "plugin.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            raise ValueError(f"插件源无法读取 plugin.json：{source}：{exc}") from exc
    if not isinstance(manifest, dict):
        raise TypeError(f"plugin.json 根值必须是对象：{source}")
    plugin_id = cast(dict[str, object], manifest).get("id")
    if not isinstance(plugin_id, str) or not plugin_id:
        raise ValueError(f"插件源缺少有效 id：{source}")
    return plugin_id


class _PluginManifest(TypedDict):
    id: str
    name: str
    version: str
    entrypoint: str
    resource_root: str | None
    config: dict[str, object]
    requires: list[str]
    kind: list[str]
    #: 数据依赖（plugin.json 的 dependencies）：跨插件读表的显式声明
    dependencies: list[DataDependency]


class _PluginRecord(_PluginManifest):
    enabled: bool
    last_error: str | None
    digest: str


class PublicPluginRecord(TypedDict):
    id: str
    name: str
    version: str
    entrypoint: str
    resource_root: str | None
    requires: list[str]
    kind: list[str]
    enabled: bool
    last_error: str | None
    digest: str
    #: 持久化的插件配置（plugin.json 的 ``config`` 与 ``update_config`` 改动的合并结果）
    config: dict[str, object]
    #: 数据依赖（plugin.json 的 ``dependencies``）：跨插件读表的显式声明
    dependencies: list[DataDependency]
    #: 是否带描述文件（插件包根目录 README.md，前端据此控制「文档」入口）
    has_readme: bool
    #: 内置插件（common/ 直装）：不可安装 / 升级 / 启停 / 卸载 / 配置修改
    builtin: bool
    #: 预装插件（config.ini 的 ``[node] plugins`` 列出的插件源）：启停与
    #: 卸载均允许，但下次启动会被 ``ensure_installed`` 重装并重置状态
    protected: bool


class PluginManagerError(ValueError):
    """可直接返回给插件管理 API 的错误。"""

    def __init__(
        self, message: str, status_code: int = 400, *, details: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code: int = status_code
        #: 结构化错误载荷（如同 id 冲突时的新旧版本），随 API 一并返回
        self.details: dict[str, object] | None = details


#: requires 字段校验失败的统一提示（形状与内容两处检查共用）
_REQUIRES_ERROR: Final[str] = "requires 必须是字符串列表（每项非空且不超过 64 字符，最多 32 项）"
#: kind 字段校验失败的统一提示
_KIND_ERROR: Final[str] = (
    f"plugin.json 必须提供 kind（{' / '.join(PLUGIN_KINDS)} 之一，单值或列表）"
)


def _validate_requires(value: object) -> list[str]:
    """校验 ``requires`` 归属元数据：非空字符串列表（最多 32 项，每项 ≤64 字符）。"""
    if value is None:
        value = []
    if not isinstance(value, list):
        raise PluginManagerError(_REQUIRES_ERROR)
    items = cast(list[object], value)
    if len(items) > MAX_REQUIRES or any(
        not isinstance(item, str) or not item.strip() or len(item) > 64
        for item in items
    ):
        raise PluginManagerError(_REQUIRES_ERROR)
    return [cast(str, item).strip() for item in items]


class PluginManager:
    """从 ZIP 安装 Python 插件包，并维护启用 fiber 与持久化状态。"""

    def __init__(
        self,
        ctx: Context,
        install_root: Path,
        preinstalled_ids: Iterable[str] = (),
    ) -> None:
        self.ctx: Context = ctx
        self.install_root: Path = install_root.resolve()
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.registry_file: Path = self.install_root / "registry.json"
        #: 原始插件包归档目录（保留上传 ZIP，供未来主从分发与回溯）
        self.archives_dir: Path = self.install_root / ".archives"
        #: 预装插件 id（config.ini 的 ``[node] plugins`` 插件源解析而来）：
        #: 启停 / 卸载均允许，但下次启动 ``ensure_installed`` 会重装重置
        self.preinstalled_ids: frozenset[str] = frozenset(preinstalled_ids)
        self.records: dict[str, _PluginRecord] = self._read_registry()
        self.fibers: dict[str, Fiber] = {}
        self._operation_lock = asyncio.Lock()

    def _builtins(self) -> dict[str, _BuiltinSpec]:
        """内置插件元数据（按已加载插件动态发现，见 :func:`builtin_specs`）。"""
        return builtin_specs(self.ctx)

    @staticmethod
    def _module_name(plugin_id: str) -> str:
        return f"_aetp_plugin_{plugin_id.encode('ascii').hex()}"

    async def list_plugins(self) -> list[PublicPluginRecord]:
        records = [self._public_record(record) for record in self.records.values()]
        return sorted([*self._builtin_records(), *records], key=lambda item: item["id"])

    def _builtin_records(self) -> list[PublicPluginRecord]:
        """内置插件的展示记录：不落 registry，状态恒为运行中。"""
        return [
            {
                "id": plugin_id,
                "name": spec["name"],
                "version": spec["version"],
                "entrypoint": spec["entrypoint"],
                "resource_root": None,
                "kind": list(spec["kind"]),
                "requires": [],
                "enabled": True,
                "last_error": None,
                "digest": "",
                "config": {},
                "dependencies": [],
                "has_readme": (spec["root"] / PLUGIN_README).is_file(),
                "builtin": True,
                "protected": True,
            }
            for plugin_id, spec in sorted(self._builtins().items())
        ]

    def _reject_builtin(self, plugin_id: str, action: str) -> None:
        """内置插件随平台直装，不经插件管理器安装，拒绝任何变装操作。"""
        if plugin_id in self._builtins():
            raise PluginManagerError(
                f"内置插件 {plugin_id!r} 随平台直装，不支持{action}", 403
            )

    async def install_archive(self, archive: bytes, *, replace: bool = False) -> PublicPluginRecord:
        """安装插件包；同 id 已安装时需显式 ``replace=True`` 才覆盖替换。"""
        async with self._operation_lock:
            return await self._install_archive(archive, replace=replace)

    async def install_source(self, source_dir: str | Path, *, replace: bool = False) -> PublicPluginRecord:
        """从本地插件源目录安装（与上传 ZIP 同一条安装链路）。

        源目录是「未安装的插件源文件」（如 ``master/webui``）；安装产物
        落到本节点安装目录，原始包归档保留，后续可经 :meth:`package_bytes`
        读出用于分发。``node_modules`` 等构建缓存不进包。
        同 id 已安装时需显式 ``replace=True`` 才覆盖替换（预装源更新换装
        走这里），否则报 409 并携带新旧版本信息。
        """
        async with self._operation_lock:
            source = Path(source_dir).expanduser().resolve()
            if not source.is_dir():
                raise PluginManagerError(f"插件源目录不存在：{source}")
            archive = await asyncio.to_thread(self._zip_directory, source)
            return await self._install_archive(archive, replace=replace)

    @staticmethod
    def _zip_directory(source: Path) -> bytes:
        """把插件源目录打包成 ZIP（排除构建缓存、依赖目录与符号链接）。"""
        buffer = BytesIO()
        total_size = 0
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
            for path in sorted(source.rglob("*")):
                relative = path.relative_to(source)
                if any(part in EXCLUDED_DIRS for part in relative.parts):
                    continue
                if path.is_symlink():
                    continue  # 不跟随符号链接：防把源外文件打进包
                if not path.is_file() or path.suffix.lower() in EXCLUDED_SUFFIXES:
                    continue
                total_size += path.stat().st_size
                if total_size > MAX_EXTRACTED_SIZE:
                    raise PluginManagerError("插件源目录内容超过 128 MiB", 413)
                package.write(path, relative.as_posix())
        return buffer.getvalue()

    async def _install_archive(self, archive: bytes, *, replace: bool = False) -> PublicPluginRecord:
        if not archive:
            raise PluginManagerError("上传文件为空")
        if len(archive) > MAX_ARCHIVE_SIZE:
            raise PluginManagerError("插件 ZIP 最大允许 32 MiB", 413)

        staging_root: Path = Path(tempfile.mkdtemp(prefix=".upload-", dir=self.install_root))
        extracted: Path = staging_root / "package"
        extracted.mkdir()
        try:
            manifest = await asyncio.to_thread(self._extract_and_read_manifest, archive, extracted)
            self._reject_builtin(manifest["id"], "安装 / 升级")
            self._check_node_kind(manifest)
            self._check_data_dependencies(manifest)
            plugin_id = manifest["id"]
            # 摘要按 zip 条目（名字 + 内容）计算：与 ensure_installed 的
            # source_digest 同口径，保证启动比对不会因口径不同而每次误换装
            digest = await asyncio.to_thread(archive_digest, archive)
            existing = self.records.get(plugin_id)
            destination = self.install_root / plugin_id
            if existing is not None or destination.exists():
                if not replace:
                    installed_version = (
                        str(existing.get("version") or "未知") if existing else "未知"
                    )
                    raise PluginManagerError(
                        f"插件 {plugin_id!r} 已安装（版本 {installed_version}），"
                        f"新包版本 {manifest['version']}；如需替换请显式确认",
                        409,
                        details={
                            "code": "already-installed",
                            "id": plugin_id,
                            "installed_version": installed_version,
                            "incoming_version": manifest["version"],
                        },
                    )
                await self._replace_install(plugin_id, manifest, extracted, archive, digest, staging_root)
                return self._public_record(self.records[plugin_id])

            # 校验全部通过后才落地；落地任一步失败（含取消）都回滚，不留孤儿数据
            archive_path = self._archive_path(plugin_id)
            record: _PluginRecord = {
                **manifest,
                "enabled": False,
                "last_error": None,
                "digest": digest,
            }
            try:
                await asyncio.to_thread(
                    self._land_files, extracted, destination, archive_path, archive
                )
                self.records[plugin_id] = record
                self._write_registry()
            except BaseException:
                self.records.pop(plugin_id, None)
                await asyncio.to_thread(
                    self._discard_landing, destination, archive_path
                )
                raise
            return self._public_record(record)
        except PluginManagerError:
            raise
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise PluginManagerError(f"插件包无法安装：{exc}") from exc
        finally:
            await asyncio.to_thread(shutil.rmtree, staging_root, ignore_errors=True)

    @staticmethod
    def _land_files(
        extracted: Path, destination: Path, archive_path: Path, archive: bytes
    ) -> None:
        """落地安装目录与归档（纯文件 IO，交线程执行）。"""
        os.replace(extracted, destination)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(archive)

    def _discard_landing(self, destination: Path, archive_path: Path) -> None:
        """清理失败落地的残留（纯文件 IO，交线程执行）。"""
        shutil.rmtree(destination, ignore_errors=True)
        archive_path.unlink(missing_ok=True)
        self._prune_archives_dir()

    async def _replace_install(
        self,
        plugin_id: str,
        manifest: _PluginManifest,
        extracted: Path,
        archive: bytes,
        digest: str,
        staging_root: Path,
    ) -> None:
        """覆盖安装同 id 插件：停旧 → 原子换目录 → 启新版；任一步失败整体回滚到旧版。

        旧版 zip 归档保留一版（``{id}.prev-{旧版本}.zip``）供回溯；原启用状态
        保留——旧版启用中则新版落地后重新启用，新版启用失败同样回滚。
        """
        record = self.records[plugin_id]
        previous: _PluginRecord = {**record}
        was_enabled = plugin_id in self.fibers
        destination = self.install_root / plugin_id
        archive_path = self._archive_path(plugin_id)
        backup_dir = staging_root / "previous"
        # 旧版归档保留一版供回滚/回溯；版本串中的非法文件名字符归一为 `_`
        old_version = re.sub(r"[^\w.-]", "_", str(record.get("version") or "unknown"))
        backup_archive = self.archives_dir / f"{plugin_id}.prev-{old_version}.zip"
        module_name = self._module_name(plugin_id)
        fiber: Fiber | None = self.fibers.pop(plugin_id, None)
        moved = False
        try:
            if fiber is not None:
                cleanup_error = await safe_dispose(fiber)
                if cleanup_error is not None:
                    self.ctx.logger.error(
                        "插件 %s 换装时清理旧版 Fiber 失败：%s", plugin_id, cleanup_error
                    )
            self._remove_modules(module_name)
            # 原子换目录：旧目录先进暂存区，新版就位后旧版随 finally 清理
            moved = await asyncio.to_thread(
                self._swap_install,
                destination,
                backup_dir,
                extracted,
                archive_path,
                archive,
                backup_archive,
            )
            self.records[plugin_id] = {
                **manifest,
                # 升级不重置用户持久化配置：旧值优先，新清单里的新键补默认值。
                # 新版本若删了旧键，启用时会显式报「未知配置项」而不是静默丢配置
                "config": {
                    **dict(manifest.get("config") or {}),
                    **dict(previous.get("config") or {}),
                },
                "enabled": was_enabled,
                "last_error": None,
                "digest": digest,
            }
            self._write_registry()
            if was_enabled:
                await self._enable(plugin_id)
        except BaseException as exc:
            # 整体回滚：目录、归档、注册表与 fiber 状态全部还原旧版
            self.fibers.pop(plugin_id, None)
            self._remove_modules(module_name)
            await asyncio.to_thread(
                self._restore_files,
                destination,
                backup_dir,
                moved,
                archive_path,
                backup_archive,
            )
            self.records[plugin_id] = {
                **previous,
                "enabled": False,
                "last_error": f"覆盖安装失败：{exc}",
            }
            try:
                self._write_registry()
                if was_enabled:
                    await self._enable(plugin_id)  # 回滚后重新启用旧版
                # _enable 成功会清空 last_error；再补上失败原因，让界面能告知替换未生效
                self.records[plugin_id] = {
                    **self.records[plugin_id],
                    "last_error": f"覆盖安装失败：{exc}",
                }
                self._write_registry()
            except OSError as registry_error:
                self.ctx.logger.error("插件 %s 回滚状态无法写入注册表：%s", plugin_id, registry_error)
            except PluginManagerError as rollback_error:
                self.ctx.logger.error("插件 %s 回滚后重新启用失败：%s", plugin_id, rollback_error)
            if isinstance(exc, asyncio.CancelledError):
                raise
            status_code = exc.status_code if isinstance(exc, PluginManagerError) else 400
            raise PluginManagerError(
                f"覆盖安装 {plugin_id!r} 失败，已回滚到旧版本：{exc}", status_code
            ) from exc

    @staticmethod
    def _swap_install(
        destination: Path,
        backup_dir: Path,
        extracted: Path,
        archive_path: Path,
        archive: bytes,
        backup_archive: Path,
    ) -> bool:
        """换装的文件操作（纯 IO，交线程执行）：旧目录进暂存、新版就位、归档留备份。

        返回旧目录是否已挪走（供回滚判断）。
        """
        moved = False
        if destination.exists():
            os.replace(destination, backup_dir)
            moved = True
        os.replace(extracted, destination)
        backup_archive.parent.mkdir(parents=True, exist_ok=True)
        if archive_path.is_file():
            backup_archive.write_bytes(archive_path.read_bytes())
        archive_path.write_bytes(archive)
        return moved

    def _restore_files(
        self,
        destination: Path,
        backup_dir: Path,
        moved: bool,
        archive_path: Path,
        backup_archive: Path,
    ) -> None:
        """回滚的文件操作（纯 IO，交线程执行）：还原旧目录与旧归档。"""
        shutil.rmtree(destination, ignore_errors=True)
        if moved:
            os.replace(backup_dir, destination)
        if backup_archive.is_file():
            archive_path.write_bytes(backup_archive.read_bytes())
            backup_archive.unlink(missing_ok=True)
        self._prune_archives_dir()

    async def enable(self, plugin_id: str) -> PublicPluginRecord:
        self._reject_builtin(plugin_id, "启停")
        async with self._operation_lock:
            return await self._enable(plugin_id)

    async def _enable(self, plugin_id: str) -> PublicPluginRecord:
        record = self._get_record(plugin_id)
        existing = self.fibers.get(plugin_id)
        if existing is not None:
            if self._fiber_state_name(existing) == "ACTIVE":
                return self._public_record(record)  # 幂等短路：仅对健康运行的 fiber
            # 死 fiber（如热重载失败残留）：就地清理后全新装载，enable 不做静默 no-op
            await self._discard_dead_fiber(plugin_id, existing)

        package_root = self.install_root / plugin_id
        module_name = self._module_name(plugin_id)
        fiber: Fiber | None = None
        try:
            await self._check_data_requirements(record)
            plugin_class = self._load_plugin_class(package_root, module_name, record["entrypoint"])
            config = dict(record.get("config") or {})
            resource_root = record.get("resource_root")
            if resource_root:
                config["resource_root"] = str((package_root / resource_root).resolve())

            fiber = self.ctx.plugin(plugin_class, config)
            await fiber
            state = self._fiber_state_name(fiber)
            if state != "ACTIVE":
                error = getattr(fiber, "_error", None)
                if error is not None:
                    raise PluginManagerError(f"插件启用失败：{error}", 422)
                missing = self._missing_injects(plugin_class)
                if missing:
                    raise PluginManagerError(
                        f"插件启用失败：依赖的服务未就绪：{', '.join(missing)}"
                        "（检查插件加载顺序或对应插件是否已启用）",
                        422,
                    )
                raise PluginManagerError(f"插件启用失败：{state or '状态未激活'}", 422)

            record["enabled"] = True
            record["last_error"] = None
            self._write_registry()
            self.fibers[plugin_id] = fiber
            return self._public_record(record)
        except asyncio.CancelledError:
            await self._rollback_failed_enable(
                plugin_id,
                module_name,
                fiber,
                "插件启用操作已取消",
            )
            raise
        except Exception as exc:
            await self._rollback_failed_enable(plugin_id, module_name, fiber, str(exc))
            if isinstance(exc, PluginManagerError):
                raise
            raise PluginManagerError(f"插件启用失败：{exc}", 422) from exc

    async def _rollback_failed_enable(
        self,
        plugin_id: str,
        module_name: str,
        fiber: Fiber | None,
        error: str,
    ) -> None:
        if fiber is not None:
            # 回滚清理尽力而为，不掩盖原始错误
            cleanup_error = await safe_dispose(fiber)
            if cleanup_error is not None:
                self.ctx.logger.error(
                    "插件 %s 启用回滚时清理 Fiber 失败：%s", plugin_id, cleanup_error
                )
        self.fibers.pop(plugin_id, None)
        self._remove_modules(module_name)
        record = self._get_record(plugin_id)
        record["enabled"] = False
        record["last_error"] = error
        try:
            self._write_registry()
        except OSError as registry_error:
            self.ctx.logger.error("插件 %s 启用失败状态无法写入注册表：%s", plugin_id, registry_error)

    @staticmethod
    def _fiber_state_name(fiber: Fiber) -> str:
        """fiber 状态名（实时）：``"ACTIVE"`` 表示健康运行。"""
        return fiber.get_state().name

    def _missing_injects(self, plugin_class: type[object]) -> list[str]:
        """插件 ``inject`` 声明里尚未就绪的服务名（诊断「依赖未就绪」用）。"""
        names = getattr(plugin_class, "inject", None) or ()
        return [
            str(name)
            for name in names
            if self.ctx.get(str(name), strict=False) is None
        ]

    async def _discard_dead_fiber(self, plugin_id: str, fiber: Fiber) -> None:
        """清理「非激活」残留 fiber（如热重载失败）：销毁并摘除模块登记，
        下次 :meth:`_enable` 全新装载；清理异常只记录，不掩盖原始错误。"""
        self.fibers.pop(plugin_id, None)
        cleanup_error = await safe_dispose(fiber)
        if cleanup_error is not None:
            self.ctx.logger.error(
                "插件 %s 清理残留 Fiber 失败：%s", plugin_id, cleanup_error
            )
        self._remove_modules(self._module_name(plugin_id))

    async def disable(self, plugin_id: str, *, reason: str | None = None) -> PublicPluginRecord:
        """停用插件；``reason`` 会记入安装记录的 ``last_error``（如数据依赖失效）。"""
        self._reject_builtin(plugin_id, "停用")
        async with self._operation_lock:
            return await self._disable(plugin_id, reason=reason)

    async def _disable(self, plugin_id: str, *, reason: str | None = None) -> PublicPluginRecord:
        record = self._get_record(plugin_id)
        fiber = self.fibers.pop(plugin_id, None)
        if fiber is not None:
            cleanup_error = await safe_dispose(fiber)
            if cleanup_error is not None:
                self.ctx.logger.error(
                    "插件 %s 停用时清理 Fiber 失败：%s", plugin_id, cleanup_error
                )
        self._remove_modules(self._module_name(plugin_id))
        record["enabled"] = False
        if reason is not None:
            record["last_error"] = reason
        self._write_registry()
        return self._public_record(record)

    async def uninstall(
        self, plugin_id: str, *, keep_data: bool = False, drop: bool = False
    ) -> None:
        """停用并删除插件包；同步清理它认领的数据表（db 就绪时）。

        * ``keep_data=True``：数据表与归属登记保留（重装复用）；
        * ``drop=True``：被其他插件依赖时强制删除数据（默认拒绝卸载）。
        """
        self._reject_builtin(plugin_id, "卸载")
        async with self._operation_lock:
            await self._uninstall(plugin_id, keep_data=keep_data, drop=drop)

    async def _uninstall(
        self, plugin_id: str, *, keep_data: bool = False, drop: bool = False
    ) -> None:
        self._get_record(plugin_id)  # 未知插件 404 校验
        if plugin_id in self.fibers:
            await self._disable(plugin_id)
        await self._drop_plugin_data(plugin_id, keep_data=keep_data, drop=drop)
        package_root = (self.install_root / plugin_id).resolve()
        if package_root.parent != self.install_root:
            raise PluginManagerError("插件安装目录无效", 400)
        try:
            await asyncio.to_thread(shutil.rmtree, package_root)
        except OSError as exc:
            raise PluginManagerError(
                f"插件 {plugin_id!r} 卸载失败：{exc.strerror or exc}"
                "（常见原因是插件文件被占用，请关闭相关进程后重试）"
            ) from exc
        self._archive_path(plugin_id).unlink(missing_ok=True)
        # 覆盖安装留下的旧版归档一并清理，避免残留孤儿 zip（id 前缀精确匹配）
        for stale in self.archives_dir.glob(f"{plugin_id}.prev-*.zip"):
            stale.unlink(missing_ok=True)
        self._prune_archives_dir()
        self.records.pop(plugin_id, None)
        self._write_registry()

    async def update_config(
        self, plugin_id: str, config: Mapping[str, object]
    ) -> PublicPluginRecord:
        """部分更新插件配置（给出要改的键即可，其余键保留）。

        * **运行中**：经 ``fiber.update`` 校验并热更新；Schema 校验失败报 422
          且不保存；``internal/commit`` 钩子把新值写回 ``config.ini`` 对应节；
        * **未启用**：只保存进安装记录，下次 ``enable`` 时生效（启用链路校验）。

        两处持久化（安装记录 + ``config.ini``）保持一致，重启后行为不变。

        抛出 :class:`PluginManagerError`：内置插件 403、未知插件 404、
        配置为空 400、校验失败 / 重载失败 422。
        """
        self._reject_builtin(plugin_id, "配置修改")
        async with self._operation_lock:
            return await self._update_config(plugin_id, config)

    async def _update_config(
        self, plugin_id: str, config: Mapping[str, object]
    ) -> PublicPluginRecord:
        record = self._get_record(plugin_id)
        if not config:
            raise PluginManagerError("配置不能为空（部分更新：给出要改的键即可）", 400)
        merged = {**dict(record.get("config") or {}), **dict(config)}
        fiber = self.fibers.get(plugin_id)
        if fiber is None:
            # 未启用：只保存，下次 enable 时生效（启用链路负责 Schema 校验）
            record["config"] = merged
            self._write_registry()
            return self._public_record(record)

        # 运行时配置与 _enable 同口径：resource_root 由安装记录注入
        runtime = dict(merged)
        resource_root = record.get("resource_root")
        if resource_root:
            package_root = self.install_root / plugin_id
            runtime["resource_root"] = str((package_root / resource_root).resolve())
        try:
            outcome = fiber.update(runtime)
        except PluginManagerError:
            raise
        except TypeError as exc:  # 含 Schema 校验失败的 ValidationError（继承 TypeError）
            raise PluginManagerError(f"配置校验失败：{exc}", 422) from exc
        except OSError as exc:  # internal/commit 写回 config.ini 失败
            raise PluginManagerError(f"配置写回 config.ini 失败：{exc}", 500) from exc
        except Exception as exc:
            raise PluginManagerError(f"配置更新失败：{exc}", 422) from exc

        # 校验已通过、internal/commit 已写回 config.ini：安装记录同步更新
        record["config"] = merged
        record["last_error"] = None
        self._write_registry()
        if outcome is None:
            return self._public_record(record)  # 重启被钩子否决

        try:
            await outcome
        except Exception as exc:
            await self._discard_dead_fiber(plugin_id, fiber)
            raise self._reload_error(record, exc) from exc
        state = self._fiber_state_name(fiber)
        if state != "ACTIVE":
            await self._discard_dead_fiber(plugin_id, fiber)
            raise self._reload_error(
                record, getattr(fiber, "_error", None) or state or "状态未激活"
            )
        return self._public_record(record)

    def _reload_error(
        self, record: _PluginRecord, reason: object
    ) -> PluginManagerError:
        """重载失败的统一处理：错误记入安装记录并构造 422（配置本身已保存）。

        残留 fiber 已就地清理，记录同步置为未启用 —— 修复配置后 ``enable``
        直接全新装载，不再依赖「先停再启」的隐式恢复路径。
        """
        message = f"配置已保存，但插件重载失败、已停用（修复配置后重新启用）：{reason}"
        record["enabled"] = False
        record["last_error"] = message
        try:
            self._write_registry()
        except OSError as registry_error:
            self.ctx.logger.error(
                "插件 %s 更新失败状态无法写入注册表：%s", record["id"], registry_error
            )
        return PluginManagerError(message, 422)

    async def restore_enabled(self) -> None:
        for plugin_id, record in list(self.records.items()):
            if not record.get("enabled"):
                continue
            record["enabled"] = False
            try:
                await self.enable(plugin_id)
            except PluginManagerError as exc:
                record["last_error"] = str(exc)
                self._write_registry()

    def _extract_and_read_manifest(self, archive: bytes, destination: Path) -> _PluginManifest:
        try:
            with zipfile.ZipFile(BytesIO(archive)) as package:
                members = package.infolist()
                if len(members) > MAX_ARCHIVE_FILES:
                    raise PluginManagerError("插件包文件数量超过 2048")
                expanded_size = 0
                for member in members:
                    path = PurePosixPath(member.filename.replace("\\", "/"))
                    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                        raise PluginManagerError(f"插件包包含非法路径：{member.filename}")
                    if path.parts and ":" in path.parts[0]:
                        raise PluginManagerError(f"插件包包含非法路径：{member.filename}")
                    if (member.external_attr >> 16) & 0o170000 == 0o120000:
                        raise PluginManagerError(f"插件包不允许符号链接：{member.filename}")
                    expanded_size += member.file_size
                    if expanded_size > MAX_EXTRACTED_SIZE:
                        raise PluginManagerError("插件解压后最大允许 128 MiB", 413)

                if "plugin.json" not in {member.filename for member in members}:
                    raise PluginManagerError("ZIP 根目录必须包含 plugin.json")
                # 上传包同样剔除构建缓存与旧字节码：旧 pyc 会让「升级后仍跑旧代码」
                for member in members:
                    if _is_excluded_entry(member.filename):
                        continue
                    package.extract(member, destination)
        except zipfile.BadZipFile as exc:
            raise PluginManagerError("上传文件不是有效 ZIP") from exc

        manifest_path = destination / "plugin.json"
        if manifest_path.stat().st_size > 64 * 1024:
            raise PluginManagerError("plugin.json 最大允许 64 KiB")
        manifest: object = json.loads(manifest_path.read_text(encoding="utf-8"))
        return self._validate_manifest(manifest, destination)

    def _validate_manifest(self, manifest: object, package_root: Path) -> _PluginManifest:
        if not isinstance(manifest, dict):
            raise PluginManagerError("plugin.json 根值必须是对象")
        manifest = cast(dict[str, object], manifest)
        plugin_id = manifest.get("id")
        name = manifest.get("name")
        version = manifest.get("version")
        entrypoint = manifest.get("entrypoint")
        if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id):
            raise PluginManagerError("plugin.json 的 id 必须是小写字母、数字、下划线或连字符")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise PluginManagerError("plugin.json 必须提供有效 name")
        if not isinstance(version, str) or not version.strip() or len(version) > 64:
            raise PluginManagerError("plugin.json 必须提供有效 version")
        if not isinstance(entrypoint, str) or not ENTRYPOINT.fullmatch(entrypoint):
            raise PluginManagerError("entrypoint 格式必须为 module:PluginClass")
        config = manifest.get("config", {})
        if not isinstance(config, dict):
            raise PluginManagerError("config 必须是 JSON 对象")
        config = cast(dict[str, object], config)

        requires = _validate_requires(manifest.get("requires", []))
        kind = self._validate_kind(manifest)
        try:
            dependencies = parse_dependencies(manifest.get("dependencies"))
        except (TypeError, ValueError) as exc:
            raise PluginManagerError(str(exc)) from exc

        resource_root = manifest.get("resource_root")
        if resource_root is not None:
            if not isinstance(resource_root, str) or not resource_root.strip():
                raise PluginManagerError("resource_root 必须是相对目录")
            resource_path = (package_root / resource_root).resolve()
            if not resource_path.is_relative_to(package_root.resolve()) or not resource_path.is_dir():
                raise PluginManagerError("resource_root 目录不存在或越出插件包")

        module_path = entrypoint.split(":", 1)[0]
        module_file = package_root.joinpath(*module_path.split(".")).with_suffix(".py")
        if not module_file.is_file():
            raise PluginManagerError(f"插件入口文件不存在：{module_file.relative_to(package_root)}")

        return {
            "id": plugin_id,
            "name": name.strip(),
            "version": version.strip(),
            "entrypoint": entrypoint,
            "resource_root": resource_root,
            "config": config,
            "requires": requires,
            "kind": kind,
            "dependencies": dependencies,
        }

    @staticmethod
    def _validate_kind(manifest: dict[str, object]) -> list[str]:
        """校验 ``kind``（目标节点类型，必填，单值或列表）并归一化为列表。

        合法值 ``master`` / ``slave`` / ``any``（不限）；``any`` 不能与其他值并列。
        """
        raw = manifest.get("kind")
        values: list[object]
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, list):
            values = cast(list[object], raw)
        else:
            raise PluginManagerError(_KIND_ERROR)
        kinds: list[str] = []
        for item in values:
            if not isinstance(item, str):
                raise PluginManagerError(_KIND_ERROR)
            kind = item.strip().lower()
            if kind not in PLUGIN_KINDS:
                raise PluginManagerError(
                    f"kind 必须是 {' / '.join(PLUGIN_KINDS)} 之一（单值或列表），收到 {item!r}"
                )
            if kind not in kinds:
                kinds.append(kind)
        if not kinds:
            raise PluginManagerError(_KIND_ERROR)
        if "any" in kinds and len(kinds) > 1:
            raise PluginManagerError("kind 为 any（不限）时不能与其他值并列")
        return kinds

    def _check_node_kind(self, manifest: _PluginManifest) -> None:
        """安装前校验插件的目标 ``kind`` 覆盖当前节点（``any`` 或列表含当前节点）。

        抛出:
            PluginManagerError: 环境没有 ``node_kind`` 服务（无法校验），
                或不支持当前节点类型。
        """
        target = manifest.get("kind") or []
        if "any" in target:
            return
        node_kind = self.ctx.get("node_kind")
        if node_kind is None:
            raise PluginManagerError(
                f"插件 {manifest['id']!r} 支持 {' / '.join(target)} 节点，"
                "当前环境没有 node_kind 服务，无法校验"
            )
        if str(node_kind) not in target:
            raise PluginManagerError(
                f"插件 {manifest['id']!r} 支持 {' / '.join(target)} 节点，"
                f"不能安装到 {node_kind!s} 节点"
            )

    def _check_data_dependencies(self, manifest: _PluginManifest) -> None:
        """安装前数据依赖预检：装 B 即提示 A 未安装（比加载拒载反馈更早）。"""
        missing = [
            dependency["plugin"]
            for dependency in manifest["dependencies"]
            if dependency["plugin"] not in self.records and dependency["plugin"] not in self._builtins()
        ]
        if missing:
            unique = ", ".join(sorted(set(missing)))
            raise PluginManagerError(
                f"插件 {manifest['id']!r} 的数据依赖未安装：{unique}；请先安装并启用生产方插件",
                409,
                details={"code": "missing-data-dependencies", "plugins": sorted(set(missing))},
            )

    async def _check_data_requirements(self, record: _PluginRecord) -> None:
        """启用前校验数据依赖（锁步模型）：表不存在 / requires 不符即拒载。

        db 未就绪（无 ``db`` 服务）时跳过——数据依赖校验是 db 插件的能力。
        """
        dependencies = list(record.get("dependencies") or [])
        if not dependencies:
            return
        db = self.ctx.get("db", strict=False)
        if db is None:
            self.ctx.logger.warning(
                "插件 %s 声明了数据依赖，但 db 服务未就绪，跳过校验", record["id"]
            )
            return
        await db.check_dependencies(record["id"], dependencies)

    async def _drop_plugin_data(
        self, plugin_id: str, *, keep_data: bool, drop: bool
    ) -> None:
        """卸载时清理插件认领的数据表（在删除插件包**之前**调用）。

        * 被其他插件依赖时默认**拒绝卸载**（``drop=True`` 强删）——抛错阻断，
          插件包不动；
        * 其余清理失败**不阻断卸载**：登记保留 + 错误日志，待孤儿回收
          （DESIGN §4.3）；
        * db 未就绪时跳过（无数据能力可清理）。
        """
        db = self.ctx.get("db", strict=False)
        if db is None:
            return
        try:
            await db.drop_owner(plugin_id, keep_data=keep_data, force=drop)
        except DbDependencyError as exc:
            raise PluginManagerError(str(exc), 409) from exc
        except (DbError, OSError, SQLAlchemyError) as exc:
            self.ctx.logger.error(
                "插件 %s 的数据清理失败（不阻断卸载，登记保留待孤儿回收）：%s",
                plugin_id,
                exc,
            )

    def _load_plugin_class(
        self, package_root: Path, package_name: str, entrypoint: str
    ) -> type[object]:
        # 合成模块名由 plugin_id 决定：重装/重复启用时必须先清掉旧模块，
        # 否则 importlib 会命中缓存，代码和资源路径都还是上一份安装的。
        self._remove_modules(package_name)
        module_path, class_name = entrypoint.split(":", 1)
        package = types.ModuleType(package_name)
        package.__path__ = [str(package_root)]
        package.__package__ = package_name
        package.__file__ = str(package_root / "__init__.py")
        sys.modules[package_name] = package
        module = importlib.import_module(f".{module_path}", package_name)
        plugin_class = getattr(module, class_name, None)
        if not isinstance(plugin_class, type):
            self._remove_modules(package_name)
            raise PluginManagerError(f"插件入口 {entrypoint!r} 没有指向类")
        return plugin_class

    def _remove_modules(self, package_name: str) -> None:
        for module_name in list(sys.modules):
            if module_name == package_name or module_name.startswith(package_name + "."):
                sys.modules.pop(module_name, None)

    def _get_record(self, plugin_id: str) -> _PluginRecord:
        if plugin_id in self._builtins():
            raise PluginManagerError(
                f"内置插件 {plugin_id!r} 随平台直装，没有安装记录", 404
            )
        record = self.records.get(plugin_id)
        if record is None:
            raise PluginManagerError(f"找不到插件：{plugin_id}", 404)
        return record

    def _archive_path(self, plugin_id: str) -> Path:
        return self.archives_dir / f"{plugin_id}.zip"

    def _prune_archives_dir(self) -> None:
        """归档目录空了就移除，保证安装根目录不残留空壳。"""
        with contextlib.suppress(OSError):
            self.archives_dir.rmdir()

    def package_path(self, plugin_id: str) -> Path:
        """取插件包原始 ZIP 的归档路径（供分发/回溯）。"""
        self._get_record(plugin_id)
        path = self._archive_path(plugin_id)
        if not path.is_file():
            raise PluginManagerError(f"插件包归档不存在：{plugin_id}", 404)
        return path

    def package_bytes(self, plugin_id: str) -> bytes:
        """读取插件包原始 ZIP 字节（主从分发时由通信插件调用）。"""
        return self.package_path(plugin_id).read_bytes()

    def package_source(self, source_dir: str | Path) -> bytes:
        """把插件源目录打成 zip 包（插件的交付形态，内容与安装包一致）。"""
        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir():
            raise PluginManagerError(f"插件源目录不存在：{source}")
        return self._zip_directory(source)

    async def plugin_readme(self, plugin_id: str) -> str:
        """读取插件描述文件（插件包根目录的 README.md，Markdown 文本）。

        描述文件讲述插件的功能、能力接口与使用方法，在插件管理页弹窗
        展示。文件名固定（无路径拼接，无穿越风险）；缺失报 404、超大
        报 413、非 UTF-8 报 400。内置插件读源码包根（``common/plugins/``）。
        """
        spec = self._builtins().get(plugin_id)
        if spec is not None:
            package_root = spec["root"]
        else:
            package_root = (self.install_root / plugin_id).resolve()
            self._get_record(plugin_id)
            if package_root.parent != self.install_root:
                raise PluginManagerError("插件安装目录无效", 400)
        path = package_root / PLUGIN_README
        if not path.is_file():
            raise PluginManagerError(
                f"插件 {plugin_id!r} 没有描述文件 {PLUGIN_README}", 404
            )
        if path.stat().st_size > MAX_README_BYTES:
            raise PluginManagerError("插件描述文件最大允许 10 MiB", 413)
        try:
            return await asyncio.to_thread(path.read_text, encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise PluginManagerError("插件描述文件不是有效的 UTF-8 文本") from exc

    def _public_record(self, record: _PluginRecord) -> PublicPluginRecord:
        return {
            "id": record["id"],
            "name": record["name"],
            "version": record["version"],
            "entrypoint": record["entrypoint"],
            "resource_root": record.get("resource_root"),
            "kind": record.get("kind") or ["any"],
            "requires": list(record.get("requires") or []),
            "enabled": bool(record.get("enabled")),
            "last_error": record.get("last_error"),
            "digest": record.get("digest") or "",
            "config": dict(record.get("config") or {}),
            "dependencies": list(record.get("dependencies") or []),
            "has_readme": (self.install_root / record["id"] / PLUGIN_README).is_file(),
            "builtin": False,
            "protected": record["id"] in self.preinstalled_ids,
        }

    def _read_registry(self) -> dict[str, _PluginRecord]:
        if not self.registry_file.exists():
            return {}
        try:
            data = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            raise RuntimeError(f"插件注册表无法读取：{exc}") from exc
        if not isinstance(data, dict):
            raise TypeError("插件注册表根值必须是对象")
        records: dict[str, _PluginRecord] = {}
        for plugin_id, raw_record in cast(dict[str, object], data).items():
            # 防影子插件：遗留注册表里的内置 id 一律丢弃（安装现已拒绝，
            # 这里清掉保护上线前的残留，避免与直装服务同名冲突）
            if plugin_id in self._builtins():
                continue
            if not isinstance(raw_record, dict):
                raise TypeError(f"插件 {plugin_id!r} 的注册表记录必须是对象")
            records[plugin_id] = cast(_PluginRecord, raw_record)
        return records

    def _write_registry(self) -> None:
        """原子写注册表（唯一临时文件 + fsync + 替换）：崩溃不半截，多实例不互踩。"""
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.registry_file.name}.",
            suffix=".tmp",
            dir=self.registry_file.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.records, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.registry_file)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


__all__: list[str] = ["PluginManager", "PluginManagerError", "PublicPluginRecord", "builtin_specs"]
