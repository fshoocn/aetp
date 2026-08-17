"""Agent 执行插件包安装器（P5.5）。

插件包由 Master 的受信任插件注册表签发引用。Agent 只按引用下载，先做
完整 SHA-256 校验，再解包到独立版本目录并加载显式入口点；不读取数据库或
项目配置中的任意 import 字符串。
"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Callable, Protocol
from urllib.request import urlopen
import uuid
import zipfile

from aetp_protocol.payloads import PluginPackageRef

from agent.plugins.errors import PluginInstallError
from agent.plugins.execution import AgentExecutionPlugin, AgentPluginRegistry


class PluginPackageInstaller(Protocol):
    """插件包安装端口。"""

    def install(self, package_ref: PluginPackageRef) -> AgentExecutionPlugin: ...


def _download(url: str) -> bytes:
    """通过 HTTP(S) 下载插件包；超时受控，生产不允许无限等待。"""
    try:
        with urlopen(url, timeout=30) as response:  # noqa: S310 - URL 来自 Master
            return response.read()
    except Exception as exc:  # noqa: BLE001 - 统一映射安装错误
        raise PluginInstallError(f"插件包下载失败: {url}: {exc}") from exc


class LocalPluginInstaller:
    """将插件包安装到 Agent 本地隔离目录。"""

    def __init__(
        self,
        root: str | Path,
        *,
        fetcher: Callable[[str], bytes] | None = None,
    ) -> None:
        self._root = Path(root).resolve()
        self._fetcher = fetcher or _download

    def install(self, package_ref: PluginPackageRef) -> AgentExecutionPlugin:
        """下载并安装插件包，返回校验后的执行插件实例。"""
        data = self._fetcher(package_ref.download_url)
        digest = hashlib.sha256(data).hexdigest()
        if digest.lower() != package_ref.sha256.lower():
            raise PluginInstallError(
                f"插件包 SHA-256 校验失败: task_type={package_ref.task_type}"
            )

        target = self._root / package_ref.task_type / package_ref.version
        staging = self._root / ".staging" / uuid.uuid4().hex
        try:
            staging.mkdir(parents=True, exist_ok=False)
            self._extract_archive(data, staging)
            sys.path.insert(0, str(staging))
            plugin = self._load_entry_point(package_ref.entry_point)
            self._validate_plugin(plugin, package_ref)

            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
            # target 路径稳定化后保留在 sys.path；旧 staging 路径已不存在，
            # 但模块已加载，后续 import 由 registry 直接持有实例。
            manifest = target / "aetp-plugin.json"
            manifest.write_text(
                json.dumps(package_ref.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            return plugin
        except PluginInstallError:
            raise
        except Exception as exc:  # noqa: BLE001 - 安装边界统一映射
            raise PluginInstallError(
                f"插件包安装失败: task_type={package_ref.task_type}: {exc}"
            ) from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def restore(self, registry: AgentPluginRegistry) -> int:
        """从本地已校验 manifest 恢复插件，返回恢复数量。"""
        if not self._root.is_dir():
            return 0
        restored = 0
        for manifest in self._root.glob("*/*/aetp-plugin.json"):
            try:
                ref = PluginPackageRef.model_validate(
                    json.loads(manifest.read_text(encoding="utf-8"))
                )
                sys.path.insert(0, str(manifest.parent))
                plugin = self._load_entry_point(ref.entry_point)
                self._validate_plugin(plugin, ref)
                registry.register_installed(plugin, replace=True)
                restored += 1
            except Exception as exc:  # noqa: BLE001 - 单个插件恢复失败不污染其他插件
                raise PluginInstallError(
                    f"本地插件恢复失败: {manifest}: {exc}"
                ) from exc
        return restored

    @staticmethod
    def _extract_archive(data: bytes, target: Path) -> None:
        """安全解包 zip/wheel，拒绝目录穿越。"""
        with tempfile.NamedTemporaryFile(suffix=".whl", delete=False) as file:
            file.write(data)
            archive_path = Path(file.name)
        try:
            if not zipfile.is_zipfile(archive_path):
                raise PluginInstallError("插件包不是有效的 wheel/zip 压缩包")
            with zipfile.ZipFile(archive_path) as archive:
                target_root = target.resolve()
                for member in archive.infolist():
                    destination = (target / member.filename).resolve()
                    if destination != target_root and target_root not in destination.parents:
                        raise PluginInstallError(
                            f"插件包包含非法路径: {member.filename}"
                        )
                archive.extractall(target)
        finally:
            archive_path.unlink(missing_ok=True)

    @staticmethod
    def _load_entry_point(entry_point: str) -> AgentExecutionPlugin:
        if entry_point.count(":") != 1:
            raise PluginInstallError(
                f"插件入口点格式错误，期望 module:attribute: {entry_point}"
            )
        module_name, attribute_name = entry_point.split(":", 1)
        if not module_name or not attribute_name:
            raise PluginInstallError(f"插件入口点为空: {entry_point}")
        try:
            module = importlib.import_module(module_name)
            factory = getattr(module, attribute_name)
            plugin = factory() if isinstance(factory, type) else factory
        except Exception as exc:  # noqa: BLE001 - 统一映射
            raise PluginInstallError(
                f"插件入口点加载失败: {entry_point}: {exc}"
            ) from exc
        return plugin

    @staticmethod
    def _validate_plugin(
        plugin: AgentExecutionPlugin, package_ref: PluginPackageRef
    ) -> None:
        if getattr(plugin, "task_type", None) != package_ref.task_type:
            raise PluginInstallError(
                "插件 task_type 与下载引用不一致: "
                f"{getattr(plugin, 'task_type', None)} != {package_ref.task_type}"
            )
        if getattr(plugin, "plugin_version", None) != package_ref.version:
            raise PluginInstallError(
                "插件当前版本与下载引用不一致: "
                f"{getattr(plugin, 'plugin_version', None)} != {package_ref.version}"
            )
        if package_ref.version not in getattr(plugin, "supported_versions", ()):
            raise PluginInstallError(
                "插件版本与下载引用不兼容: "
                f"{package_ref.version} not in "
                f"{sorted(getattr(plugin, 'supported_versions', ())) }"
            )