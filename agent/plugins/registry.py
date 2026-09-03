"""Agent  插件元数据注册表。"""

from __future__ import annotations

import logging
from pathlib import Path

from aetp_protocol.plugin_types import PluginRef
from aetp_protocol.plugins import PluginManifest

from agent.plugins.installer import InstalledPlugin

logger = logging.getLogger(__name__)


class PluginRegistry:
    """保存已安装  版本元数据，不导入或执行插件入口。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self._plugins: dict[tuple[str, str], InstalledPlugin] = {}
        self._root = Path(root).resolve() if root is not None else None
        if self._root is not None:
            self.restore()

    def register(self, plugin: InstalledPlugin) -> None:
        key = (plugin.ref.plugin_id.root, plugin.ref.version.root)
        existing = self._plugins.get(key)
        if existing is not None and existing.ref != plugin.ref:
            raise ValueError("本地  插件注册表摘要冲突")
        self._plugins[key] = plugin

    def remove(self, plugin_id: str, version: str) -> None:
        self._plugins.pop((plugin_id, version), None)

    def get(self, plugin_id: str, version: str) -> InstalledPlugin | None:
        return self._plugins.get((plugin_id, version))

    def list(self) -> tuple[InstalledPlugin, ...]:
        return tuple(self._plugins[key] for key in sorted(self._plugins))

    def restore(self) -> int:
        """从本地  版本目录恢复已校验元数据，不加载入口代码。"""
        if self._root is None or not self._root.is_dir():
            return 0
        restored = 0
        for marker in self._root.glob("*/*/plugin-ref.json"):
            try:
                target = marker.parent.resolve()
                target.relative_to(self._root)
                ref = PluginRef.model_validate_json(marker.read_text(encoding="utf-8"))
                manifest_path = target / "plugin.json"
                manifest = PluginManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
                if manifest.id != ref.plugin_id or manifest.version != ref.version:
                    raise ValueError("本地  Manifest 与 plugin-ref 不一致")
                self.register(InstalledPlugin(ref, manifest_path, target))
                restored += 1
            except Exception as exc:  # noqa: BLE001 - 单个损坏版本不污染其他库存
                self._record_restore_error(marker, exc)
        return restored

    @staticmethod
    def _record_restore_error(marker: Path, error: Exception) -> None:
        logger.warning("本地  插件元数据恢复失败: marker=%s error=%s", marker, error)
