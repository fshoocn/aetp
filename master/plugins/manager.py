"""受控插件包生命周期管理。

上传的 wheel 只进入 Master 运行目录的受控目录；不会在当前进程热导入。
安装、启用/停用和删除均要求平台管理员，启用后的插件在 Master 重启时由
``entry_points`` 发现。这样避免运行中的任务被动态替换代码。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from aetp_protocol.plugin import AgentPackageSpec, PluginPackage


@dataclass
class ManagedPlugin:
    plugin_id: str
    filename: str
    task_type: str
    version: str
    sha256: str
    enabled: bool = True
    installed: bool = False


class PluginManager:
    """管理 ZIP 插件的上传、安装、启停、删除和启动加载。"""

    MAX_SIZE = 100 * 1024 * 1024

    def __init__(
        self,
        root: Path,
        *,
        agent_download_builder: Callable[[str], str] | None = None,
    ) -> None:
        self.root = root / "plugins"
        self.archives = self.root / "archives"
        self.install_dir = self.root / "packages"
        self.manifest_path = self.root / "manifest.json"
        self._agent_download_builder = agent_download_builder
        self.archives.mkdir(parents=True, exist_ok=True)
        self.install_dir.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[ManagedPlugin]:
        return sorted(self._load().values(), key=lambda item: (item.task_type, item.version))

    def upload(self, filename: str, content: bytes) -> ManagedPlugin:
        if not filename.lower().endswith(".zip"):
            raise ValueError("仅支持 ZIP 插件包（.zip）")
        if len(content) > self.MAX_SIZE:
            raise ValueError("插件包不能超过 100 MB")
        safe_name = Path(filename).name
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+\.zip", safe_name):
            raise ValueError("插件文件名不合法")
        metadata = self._inspect_zip(content)
        digest = hashlib.sha256(content).hexdigest()
        plugin_id = f"{metadata['task_type']}@{metadata['version']}"
        records = self._load()
        existing = records.get(plugin_id)
        if existing is not None and existing.sha256 != digest:
            raise ValueError(f"插件版本已存在但 SHA-256 不同: {plugin_id}")
        record = ManagedPlugin(plugin_id, safe_name, metadata["task_type"], metadata["version"], digest)
        if existing is not None:
            record.enabled = existing.enabled
            record.installed = existing.installed
        (self.archives / f"{digest}.zip").write_bytes(content)
        records[plugin_id] = record
        self._save(records)
        return record

    def install(self, plugin_id: str) -> ManagedPlugin:
        record = self._get(plugin_id)
        archive = self.archives / f"{record.sha256}.zip"
        destination = self.install_dir / self._safe_id(plugin_id)
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        with zipfile.ZipFile(archive) as package:
            self._extract_safe(package, destination)
        loaded = self._load_main(destination, plugin_id)
        if not isinstance(loaded, PluginPackage):
            raise ValueError("main.py 必须导出 PluginPackage 类型的 package")
        if loaded.metadata.task_type != record.task_type or loaded.metadata.plugin_version != record.version:
            raise ValueError("main.py 的 PluginPackage 元数据与 plugin.json 不一致")
        record.installed = True
        records = self._load()
        records[plugin_id] = record
        self._save(records)
        return record

    def set_enabled(self, plugin_id: str, enabled: bool) -> ManagedPlugin:
        record = self._get(plugin_id)
        record.enabled = enabled
        records = self._load()
        records[plugin_id] = record
        self._save(records)
        return record

    def delete(self, plugin_id: str) -> None:
        record = self._get(plugin_id)
        if record.enabled:
            raise ValueError("请先停用插件后再删除")
        shutil.rmtree(self.install_dir / self._safe_id(plugin_id), ignore_errors=True)
        (self.archives / f"{record.sha256}.zip").unlink(missing_ok=True)
        records = self._load()
        records.pop(plugin_id, None)
        self._save(records)

    def load_packages(self) -> list[PluginPackage]:
        """加载已安装且启用的 ZIP 插件；仅在 Master 启动时调用。

        为每个已安装插件注入 ``agent_package``（可分发元数据）：Agent 派发
        时携带 ``plugin_ref``（签名下载 URL + sha256 + entry_point），
        Agent 检查本地版本、缺失时下载安装（§18.8）。
        """
        packages: list[PluginPackage] = []
        for record in self.list():
            if not record.enabled or not record.installed:
                continue
            destination = self.install_dir / self._safe_id(record.plugin_id)
            package = self._load_main(destination, record.plugin_id)
            if not isinstance(package, PluginPackage):
                raise ValueError(f"插件 {record.plugin_id} 的 main.py 未导出有效 package")
            package = self._with_agent_package(package, record)
            packages.append(package)
        return packages

    def _with_agent_package(
        self, package: PluginPackage, record: ManagedPlugin
    ) -> PluginPackage:
        """为已安装 ZIP 插件构造可分发 Agent 包元数据（含签名下载 URL）。"""
        if package.metadata.agent_package is not None:
            return package
        download_url = (
            self._agent_download_builder(record.plugin_id)
            if self._agent_download_builder is not None
            else ""
        )
        metadata = package.metadata
        import dataclasses

        metadata = dataclasses.replace(
            metadata,
            agent_package=AgentPackageSpec(
                package_name=record.filename,
                version=record.version,
                download_url=download_url,
                sha256=record.sha256,
                entry_point="main.py:package",
            ),
        )
        return PluginPackage(
            metadata=metadata,
            master=package.master,
            agent=package.agent,
        )

    def disabled_task_types(self) -> set[str]:
        return {item.task_type for item in self.list() if not item.enabled}

    def _inspect_zip(self, content: bytes) -> dict[str, str]:
        import io

        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            self._validate_names(names)
            if "main.py" not in names or "plugin.json" not in names:
                raise ValueError("ZIP 必须包含根目录 main.py 和 plugin.json")
            try:
                metadata = json.loads(archive.read("plugin.json"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("plugin.json 不是有效 JSON") from exc
        task_type = metadata.get("task_type")
        version = metadata.get("plugin_version")
        if not isinstance(task_type, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", task_type):
            raise ValueError("plugin.json.task_type 不合法")
        if not isinstance(version, str) or not version:
            raise ValueError("plugin.json.plugin_version 必填")
        return {"task_type": task_type, "version": version}

    @staticmethod
    def _validate_names(names: list[str]) -> None:
        for name in names:
            path = Path(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("ZIP 包含不安全的路径")

    def _extract_safe(self, archive: zipfile.ZipFile, destination: Path) -> None:
        names = archive.namelist()
        self._validate_names(names)
        for name in names:
            target = destination / name
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open("wb") as target_file:
                    shutil.copyfileobj(source, target_file)

    @staticmethod
    def _load_main(destination: Path, plugin_id: str) -> Any:
        main_path = destination / "main.py"
        module_name = f"aetp_zip_plugin_{re.sub(r'[^A-Za-z0-9_]', '_', plugin_id)}"
        if str(destination) not in sys.path:
            sys.path.insert(0, str(destination))
        spec = importlib.util.spec_from_file_location(module_name, main_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"无法加载插件入口: {plugin_id}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, "package", None)

    @staticmethod
    def _safe_id(plugin_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", plugin_id)

    def _load(self) -> dict[str, ManagedPlugin]:
        if not self.manifest_path.exists():
            return {}
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return {key: ManagedPlugin(**value) for key, value in data.items()}

    def _save(self, records: dict[str, ManagedPlugin]) -> None:
        self.manifest_path.write_text(json.dumps({key: asdict(value) for key, value in records.items()}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _get(self, plugin_id: str) -> ManagedPlugin:
        record = self._load().get(plugin_id)
        if record is None:
            raise KeyError(f"插件不存在: {plugin_id}")
        return record