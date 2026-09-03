"""Agent  插件归档安装器。"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from aetp_protocol.ids import PluginId, SemVer
from aetp_protocol.plugin_archive import PluginArchiveVerifier
from aetp_protocol.plugin_types import PluginDistributionRef, PluginRef

from agent.plugins.errors import PluginInstallError
from common.zip_utils import safe_extract_zip


@dataclass(frozen=True)
class InstalledPlugin:
    """Agent 本地已安装的  插件版本。"""

    ref: PluginRef
    manifest_path: Path
    install_path: Path


class PluginInstaller:
    """下载、校验并原子安装  插件，不加载插件代码。"""

    def __init__(
        self,
        root: str | Path,
        *,
        fetcher: Callable[[str], bytes] | None = None,
    ) -> None:
        self._root = Path(root).resolve()
        self._fetcher = fetcher or self._download
        self._verifier = PluginArchiveVerifier()

    def install(self, package: PluginDistributionRef) -> InstalledPlugin:
        if package.download_url is None:
            raise PluginInstallError(" 插件分发引用缺少下载地址")
        try:
            data = self._fetcher(package.download_url)
            digest = hashlib.sha256(data).hexdigest()
            if digest != package.archive_sha256.root:
                raise PluginInstallError("插件包 SHA-256 校验失败")
            verified = self._verifier.verify(data)
            if verified.manifest.id != package.plugin_id or verified.manifest.version != package.version:
                raise PluginInstallError("插件 Manifest 与分发引用不一致")
            ref = PluginRef(
                plugin_id=package.plugin_id,
                version=package.version,
                archive_sha256=package.archive_sha256,
            )
            target = self._root / package.plugin_id.root / package.version.root
            marker = target / "plugin-ref.json"
            if target.exists():
                if not marker.is_file():
                    raise PluginInstallError("本地  插件版本目录缺少不可变标记")
                existing = PluginRef.model_validate_json(marker.read_text(encoding="utf-8"))
                if existing != ref:
                    raise PluginInstallError("本地  插件版本不可变摘要冲突")
                self._validate_installed_target(target, verified.manifest)
                return InstalledPlugin(ref, target / "plugin.json", target)

            staging = self._root / ".staging" / uuid.uuid4().hex
            try:
                staging.mkdir(parents=True, exist_ok=False)
                safe_extract_zip(data, staging, require_root_files=["plugin.json"])
                (staging / "plugin-ref.json").write_text(ref.model_dump_json(), encoding="utf-8")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            return InstalledPlugin(ref, target / "plugin.json", target)
        except PluginInstallError:
            raise
        except Exception as exc:  # noqa: BLE001 - 安装边界统一映射
            raise PluginInstallError(f" 插件安装失败: {package.plugin_id.root}@{package.version.root}") from exc

    @staticmethod
    def _validate_installed_target(target: Path, manifest) -> None:
        """复核已有不可变目录，避免 marker 正确但入口文件被篡改。"""
        manifest_path = target / "plugin.json"
        if not manifest_path.is_file():
            raise PluginInstallError("本地  插件版本目录缺少 plugin.json")
        installed_manifest = type(manifest).model_validate_json(manifest_path.read_text(encoding="utf-8"))
        if installed_manifest != manifest:
            raise PluginInstallError("本地  Manifest 与下载归档不一致")
        for side, entrypoint in (("agent", manifest.entrypoints.agent), ("master", manifest.entrypoints.master)):
            if entrypoint is None:
                continue
            module_name, _attribute = entrypoint.root.split(":", 1)
            entry_path = (target / side / (module_name.replace(".", "/") + ".py")).resolve()
            try:
                entry_path.relative_to((target / side).resolve())
            except ValueError as exc:
                raise PluginInstallError(f"本地  {side} 入口越界") from exc
            if not entry_path.is_file():
                raise PluginInstallError(f"本地  {side} 入口文件缺失")

    def remove(self, plugin_id: PluginId, version: SemVer) -> None:
        target = self._root / plugin_id.root / version.root
        if not target.exists():
            return
        marker = target / "plugin-ref.json"
        if not marker.is_file():
            raise PluginInstallError("本地  插件版本目录缺少不可变标记")
        existing = PluginRef.model_validate_json(marker.read_text(encoding="utf-8"))
        if existing.plugin_id != plugin_id or existing.version != version:
            raise PluginInstallError("移除引用与本地  插件版本不一致")
        shutil.rmtree(target)

    @staticmethod
    def _download(url: str) -> bytes:
        try:
            with urlopen(url, timeout=30) as response:  # noqa: S310 - URL 来自 Master
                return response.read()
        except Exception as exc:  # noqa: BLE001 - 统一映射安装错误
            raise PluginInstallError(f" 插件包下载失败: {exc}") from exc
