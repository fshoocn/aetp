"""Master Reporter/Analyzer 插件入口解析器（统一经 common.plugin_loader 加载）。"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from aetp_protocol.plugin_types import PluginPoint

from common.plugin_loader import load_entrypoint
from common.zip_utils import safe_extract_zip
from master.domain.models import PluginVersionRecord
from master.plugins.registry import PluginRegistry

logger = logging.getLogger(__name__)

# point → 该扩展点 Master 入口必须提供的方法（resolve 时校验）。
# executor 的 Master 面由 ScriptDefinitionService 显式传 required_method="parse_cases"，
# 不在此列默认值。
_DEFAULT_METHOD_BY_POINT: dict[PluginPoint, str] = {
    PluginPoint.REPORTER: "report",
    PluginPoint.ANALYZER: "analyze",
    PluginPoint.NOTIFIER: "send",
    PluginPoint.HOOK: "check",
    PluginPoint.SHARDING: "split",
    PluginPoint.INTEGRATION: "execute",
}


@dataclass(frozen=True)
class ResolvedMasterExtension:
    plugin_id: str
    plugin_version: str
    plugin: object


class ExtensionResolver:
    """从已启用且已校验的  归档加载 Master 扩展。"""

    def __init__(self, registry: PluginRegistry, extraction_root: str | Path) -> None:
        self._registry = registry
        self._extraction_root = Path(extraction_root).resolve()
        self._loaded: dict[tuple[str, str, PluginPoint], ResolvedMasterExtension] = {}

    def resolve_all(self, point: PluginPoint) -> tuple[ResolvedMasterExtension, ...]:
        extensions: list[ResolvedMasterExtension] = []
        for record in self._registry.list(point):
            try:
                extensions.append(self.resolve(record, point))
            except Exception:
                logger.exception(
                    "Master  扩展加载失败: point=%s plugin=%s@%s",
                    point.value,
                    record.plugin_id.root,
                    record.version.root,
                )
        return tuple(extensions)

    def resolve(
        self,
        record: PluginVersionRecord,
        point: PluginPoint,
        *,
        required_method: str | None = None,
    ) -> ResolvedMasterExtension:
        key = (record.plugin_id.root, record.version.root, point)
        existing = self._loaded.get(key)
        if existing is not None:
            return existing
        if record.point is not point:
            raise ValueError(f" 插件扩展点不匹配: {record.point.value} != {point.value}")
        manifest = record.manifest
        entrypoint = manifest.entrypoints.master
        if entrypoint is None:
            raise ValueError(f" {point.value} 插件缺少 master entrypoint")
        install_root = self._ensure_extracted(record)
        master_root = (install_root / "master").resolve()
        if not master_root.is_dir():
            raise FileNotFoundError(f" Master 插件缺少 master 目录: {master_root}")
        _module, factory = load_entrypoint(master_root, entrypoint.root)
        plugin = factory()
        method = required_method or _DEFAULT_METHOD_BY_POINT.get(point)
        if method is None or not callable(getattr(plugin, method, None)):
            raise TypeError(f" {point.value} 入口未提供 {method}()")
        resolved = ResolvedMasterExtension(record.plugin_id.root, record.version.root, plugin)
        self._loaded[key] = resolved
        return resolved

    def install_root(self, record: PluginVersionRecord) -> Path:
        """确保插件归档已按摘要提取，返回安装根目录。

        UI 等纯静态扩展点没有 Master/Agent 入口，无法经 ``resolve()`` 加载；它们
        直接读取安装目录里的静态文件，复用同一份按摘要校验的提取缓存。
        """
        return self._ensure_extracted(record)

    def invalidate_all(self) -> None:
        """清空已解析缓存（热插拔：registry 变更后让下一次 resolve 重新加载）。"""
        self._loaded.clear()

    def _ensure_extracted(self, record: PluginVersionRecord) -> Path:
        target = (
            self._extraction_root
            / _safe_component(record.plugin_id.root)
            / _safe_component(record.version.root)
        )
        marker = target / ".archive-sha256"
        if marker.is_file() and marker.read_text(encoding="ascii").strip() == record.archive_sha256.root:
            return target
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        archive_path = Path(record.archive_path).resolve()
        with zipfile.ZipFile(archive_path) as archive:
            safe_extract_zip(archive, target)
        actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if actual != record.archive_sha256.root:
            shutil.rmtree(target, ignore_errors=True)
            raise ValueError(" 插件归档摘要在加载时发生变化")
        marker.write_text(actual, encoding="ascii")
        return target


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


__all__ = ["ExtensionResolver", "ResolvedMasterExtension"]
