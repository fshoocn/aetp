"""本地可信插件包的安装与生命周期管理。"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import types
import zipfile
from collections.abc import Coroutine
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Final, TypedDict, cast

from cordis_port import Context, Fiber

MAX_ARCHIVE_SIZE: Final[int] = 32 * 1024 * 1024
MAX_EXTRACTED_SIZE: Final[int] = 128 * 1024 * 1024
MAX_ARCHIVE_FILES: Final[int] = 2048
MAX_REQUIRES: Final[int] = 32
PLUGIN_ID: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ENTRYPOINT: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):([A-Za-z_][A-Za-z0-9_]*)$")
#: 插件归属角色：``platform`` 装在主节点（默认），``executor`` 装在执行测试的节点。
#: 当前仅作为元数据记录与展示；按角色分发到从节点由后续的主从通信插件负责。
PLUGIN_ROLES: Final[tuple[str, ...]] = ("platform", "executor")
#: 安装/归档时跳过的目录（构建缓存与依赖，不属于插件包内容）
EXCLUDED_DIRS: Final[frozenset[str]] = frozenset(
    {"node_modules", "__pycache__", ".git", ".venv", "venv"}
)
#: 安装/归档时跳过的文件后缀
EXCLUDED_SUFFIXES: Final[tuple[str, ...]] = (".pyc", ".pyo")


class _PluginManifest(TypedDict):
    id: str
    name: str
    version: str
    entrypoint: str
    resource_root: str | None
    config: dict[str, object]
    role: str
    requires: list[str]


class _PluginRecord(_PluginManifest):
    enabled: bool
    last_error: str | None


class PublicPluginRecord(TypedDict):
    id: str
    name: str
    version: str
    entrypoint: str
    resource_root: str | None
    role: str
    requires: list[str]
    enabled: bool
    last_error: str | None


class PluginManagerError(ValueError):
    """可直接返回给插件管理 API 的错误。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code: int = status_code


class PluginManager:
    """从 ZIP 安装 Python 插件包，并维护启用 fiber 与持久化状态。"""

    def __init__(self, ctx: Context, loop: asyncio.AbstractEventLoop, install_root: Path) -> None:
        self.ctx: Context = ctx
        self.loop: asyncio.AbstractEventLoop = loop
        self.install_root: Path = install_root.resolve()
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.registry_file: Path = self.install_root / "registry.json"
        #: 原始插件包归档目录（保留上传 ZIP，供未来主从分发与回溯）
        self.archives_dir: Path = self.install_root / ".archives"
        self.records: dict[str, _PluginRecord] = self._read_registry()
        self.fibers: dict[str, Fiber] = {}
        self._operation_lock = asyncio.Lock()

    @staticmethod
    def _module_name(plugin_id: str) -> str:
        return f"_aetp_plugin_{plugin_id.encode('ascii').hex()}"

    def run_from_web_thread(self, coroutine: Coroutine[object, object, object]) -> object:
        """将 Web 线程中的管理请求提交给创建 Cordis Context 的主 loop。"""
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=60)
        except TimeoutError:
            future.cancel()
            raise PluginManagerError("插件操作超时", 504) from None

    async def list_plugins(self) -> list[PublicPluginRecord]:
        return [self._public_record(record) for record in sorted(self.records.values(), key=lambda item: item["id"])]

    async def install_archive(self, archive: bytes) -> PublicPluginRecord:
        async with self._operation_lock:
            return await self._install_archive(archive)

    async def install_source(self, source_dir: str | Path) -> PublicPluginRecord:
        """从本地插件源目录安装（与上传 ZIP 同一条安装链路）。

        源目录是「未安装的插件源文件」（如 ``masterplugins/webui``）；安装产物
        落到本节点安装目录，原始包归档保留，后续可经 :meth:`package_bytes`
        读出用于分发。``node_modules`` 等构建缓存不进包。
        """
        async with self._operation_lock:
            source = Path(source_dir).expanduser().resolve()
            if not source.is_dir():
                raise PluginManagerError(f"插件源目录不存在：{source}")
            return await self._install_archive(self._zip_directory(source))

    @staticmethod
    def _zip_directory(source: Path) -> bytes:
        """把插件源目录打包成 ZIP（排除构建缓存与依赖目录）。"""
        buffer = BytesIO()
        total_size = 0
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
            for path in sorted(source.rglob("*")):
                relative = path.relative_to(source)
                if any(part in EXCLUDED_DIRS for part in relative.parts):
                    continue
                if not path.is_file() or path.suffix.lower() in EXCLUDED_SUFFIXES:
                    continue
                total_size += path.stat().st_size
                if total_size > MAX_EXTRACTED_SIZE:
                    raise PluginManagerError("插件源目录内容超过 128 MiB", 413)
                package.write(path, relative.as_posix())
        return buffer.getvalue()

    async def _install_archive(self, archive: bytes) -> _PublicPluginRecord:
        if not archive:
            raise PluginManagerError("上传文件为空")
        if len(archive) > MAX_ARCHIVE_SIZE:
            raise PluginManagerError("插件 ZIP 最大允许 32 MiB", 413)

        staging_root: Path = Path(tempfile.mkdtemp(prefix=".upload-", dir=self.install_root))
        extracted: Path = staging_root / "package"
        extracted.mkdir()
        try:
            manifest = self._extract_and_read_manifest(archive, extracted)
            plugin_id = manifest["id"]
            if plugin_id in self.records or (self.install_root / plugin_id).exists():
                raise PluginManagerError(f"插件 {plugin_id!r} 已安装", 409)

            destination = self.install_root / plugin_id
            os.replace(extracted, destination)
            archive_path = self._archive_path(plugin_id)
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            archive_path.write_bytes(archive)
            record: _PluginRecord = {
                **manifest,
                "enabled": False,
                "last_error": None,
            }
            self.records[plugin_id] = record
            try:
                self._write_registry()
            except Exception:
                self.records.pop(plugin_id, None)
                shutil.rmtree(destination, ignore_errors=True)
                archive_path.unlink(missing_ok=True)
                self._prune_archives_dir()
                raise
            return self._public_record(record)
        except PluginManagerError:
            raise
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PluginManagerError(f"插件包无法安装：{exc}") from exc
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    async def enable(self, plugin_id: str) -> PublicPluginRecord:
        async with self._operation_lock:
            return await self._enable(plugin_id)

    async def _enable(self, plugin_id: str) -> _PublicPluginRecord:
        record = self._get_record(plugin_id)
        if plugin_id in self.fibers:
            return self._public_record(record)

        package_root = self.install_root / plugin_id
        module_name = self._module_name(plugin_id)
        fiber: Fiber | None = None
        try:
            plugin_class = self._load_plugin_class(package_root, module_name, record["entrypoint"])
            config = dict(record.get("config") or {})
            resource_root = record.get("resource_root")
            if resource_root:
                config["resource_root"] = str((package_root / resource_root).resolve())

            fiber = self.ctx.plugin(plugin_class, config)
            await fiber
            state = getattr(getattr(fiber, "state", None), "name", "")
            if state != "ACTIVE":
                error = getattr(fiber, "_error", None)
                raise PluginManagerError(f"插件启用失败：{error or state or '状态未激活'}", 422)

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
            try:
                await fiber.dispose()
            except Exception as cleanup_error:  # noqa: BLE001 - 回滚清理尽力而为，不掩盖原始错误
                self.ctx.logger.error("插件 %s 启用回滚时清理 Fiber 失败：%s", plugin_id, cleanup_error)
        self.fibers.pop(plugin_id, None)
        self._remove_modules(module_name)
        record = self._get_record(plugin_id)
        record["enabled"] = False
        record["last_error"] = error
        try:
            self._write_registry()
        except OSError as registry_error:
            self.ctx.logger.error("插件 %s 启用失败状态无法写入注册表：%s", plugin_id, registry_error)

    async def disable(self, plugin_id: str) -> PublicPluginRecord:
        async with self._operation_lock:
            return await self._disable(plugin_id)

    async def _disable(self, plugin_id: str) -> _PublicPluginRecord:
        record = self._get_record(plugin_id)
        fiber = self.fibers.pop(plugin_id, None)
        if fiber is not None:
            await fiber.dispose()
        self._remove_modules(self._module_name(plugin_id))
        record["enabled"] = False
        self._write_registry()
        return self._public_record(record)

    async def uninstall(self, plugin_id: str) -> None:
        async with self._operation_lock:
            await self._uninstall(plugin_id)

    async def _uninstall(self, plugin_id: str) -> None:
        record = self._get_record(plugin_id)
        if plugin_id in self.fibers:
            await self._disable(plugin_id)
        package_root = (self.install_root / plugin_id).resolve()
        if package_root.parent != self.install_root:
            raise PluginManagerError("插件安装目录无效", 400)
        shutil.rmtree(package_root)
        self._archive_path(plugin_id).unlink(missing_ok=True)
        self._prune_archives_dir()
        self.records.pop(plugin_id, None)
        self._write_registry()
        if record.get("enabled"):
            record["enabled"] = False

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
                package.extractall(destination)
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

        role, requires = self._validate_role(manifest)

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
            "role": role,
            "requires": requires,
        }

    @staticmethod
    def _validate_role(manifest: dict[str, object]) -> tuple[str, list[str]]:
        """校验 ``role`` 与 ``requires`` 归属元数据（都可省略）。"""
        role = manifest.get("role", "platform")
        if role is None:
            role = "platform"
        if not isinstance(role, str) or role not in PLUGIN_ROLES:
            raise PluginManagerError(
                f"role 必须是 {' / '.join(PLUGIN_ROLES)} 之一，收到 {role!r}"
            )
        requires_raw = manifest.get("requires", [])
        if requires_raw is None:
            requires_raw = []
        if (
            not isinstance(requires_raw, list)
            or len(requires_raw) > MAX_REQUIRES
            or any(
                not isinstance(item, str) or not item.strip() or len(item) > 64
                for item in requires_raw
            )
        ):
            raise PluginManagerError(
                "requires 必须是字符串列表（每项非空且不超过 64 字符，最多 32 项）"
            )
        return role, [cast(str, item).strip() for item in cast(list[object], requires_raw)]

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

    def _public_record(self, record: _PluginRecord) -> PublicPluginRecord:
        return {
            "id": record["id"],
            "name": record["name"],
            "version": record["version"],
            "entrypoint": record["entrypoint"],
            "resource_root": record.get("resource_root"),
            "role": record.get("role", "platform"),
            "requires": list(record.get("requires") or []),
            "enabled": bool(record.get("enabled")),
            "last_error": record.get("last_error"),
        }

    def _read_registry(self) -> dict[str, _PluginRecord]:
        if not self.registry_file.exists():
            return {}
        try:
            data = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"插件注册表无法读取：{exc}") from exc
        if not isinstance(data, dict):
            raise TypeError("插件注册表根值必须是对象")
        records: dict[str, _PluginRecord] = {}
        for plugin_id, raw_record in cast(dict[str, object], data).items():
            if not isinstance(raw_record, dict):
                raise TypeError(f"插件 {plugin_id!r} 的注册表记录必须是对象")
            records[plugin_id] = cast(_PluginRecord, raw_record)
        return records

    def _write_registry(self) -> None:
        temporary = self.registry_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.records, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.registry_file)


__all__: list[str] = ["PluginManager", "PluginManagerError", "PublicPluginRecord"]