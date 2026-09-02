"""Master V2 Reporter/Analyzer 插件入口解析器。"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from aetp_protocol.plugin_types import PluginPoint

from common.zip_utils import safe_extract_zip
from master.domain.models import PluginVersionRecord
from master.plugins.v2_registry import V2PluginRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedMasterExtension:
    plugin_id: str
    plugin_version: str
    plugin: object


class MasterV2ExtensionResolver:
    """从已启用且已校验的 V2 归档加载 Master 扩展。"""

    def __init__(self, registry: V2PluginRegistry, extraction_root: str | Path) -> None:
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
                    "Master V2 扩展加载失败: point=%s plugin=%s@%s",
                    point.value,
                    record.plugin_id.root,
                    record.version.root,
                )
        return tuple(extensions)

    def resolve(self, record: PluginVersionRecord, point: PluginPoint) -> ResolvedMasterExtension:
        key = (record.plugin_id.root, record.version.root, point)
        existing = self._loaded.get(key)
        if existing is not None:
            return existing
        if record.point is not point:
            raise ValueError(f"V2 插件扩展点不匹配: {record.point.value} != {point.value}")
        manifest = record.manifest
        entrypoint = manifest.entrypoints.master
        if entrypoint is None:
            raise ValueError(f"V2 {point.value} 插件缺少 master entrypoint")
        install_root = self._ensure_extracted(record)
        module_name, attribute_name = entrypoint.root.split(":", 1)
        master_root = (install_root / "master").resolve()
        module_path = (master_root / (module_name.replace(".", "/") + ".py")).resolve()
        try:
            module_path.relative_to(master_root)
        except ValueError as exc:
            raise ValueError("V2 Master entrypoint 越界") from exc
        if not module_path.is_file():
            raise FileNotFoundError(f"V2 Master 入口文件不存在: {module_path}")
        module = self._load_module(module_path, key)
        factory = getattr(module, attribute_name, None)
        if not callable(factory):
            raise TypeError(f"V2 Master entrypoint 不可调用: {entrypoint.root}")
        plugin = factory()
        method = {PluginPoint.REPORTER: "report", PluginPoint.ANALYZER: "analyze"}.get(point)
        if method is None or not callable(getattr(plugin, method, None)):
            raise TypeError(f"V2 {point.value} 入口未提供 {method}()")
        resolved = ResolvedMasterExtension(record.plugin_id.root, record.version.root, plugin)
        self._loaded[key] = resolved
        return resolved

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
            raise ValueError("V2 插件归档摘要在加载时发生变化")
        marker.write_text(actual, encoding="ascii")
        return target

    @staticmethod
    def _load_module(path: Path, key: tuple[str, str, PluginPoint]) -> ModuleType:
        module_name = (
            f"aetp_v2_master_{key[2].value}_"
            f"{key[0].replace('.', '_')}_{key[1].replace('.', '_')}"
        )
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载 V2 Master 扩展: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


__all__ = ["MasterV2ExtensionResolver", "ResolvedMasterExtension"]
