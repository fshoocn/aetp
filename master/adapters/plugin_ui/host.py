"""Master 插件 UI 静态托管适配层。

UI 插件（point=ui）在归档里携带 ``ui/`` 目录（入口 HTML/JS/CSS，入口经
``entrypoints.ui`` 声明、必须位于 ``ui/`` 下）。Master 从已启用插件归档提取后，
由 ``PluginUiHost`` 提供安全的文件解析，Web Shell 以同源 iframe 加载这些静态
资源。本层只负责路径安全与文档解析，不解析 Web 与插件的消息内容（见规范
§6.3 消息协议，消息在 Web 宿主侧处理）。
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from aetp_protocol.ids import PluginId, SemVer
from aetp_protocol.plugin_types import PluginPoint

from master.domain.models import PluginVersionRecord
from master.plugins.extension_resolver import ExtensionResolver
from master.plugins.registry import PluginRegistry

logger = logging.getLogger(__name__)

_UI_DIR = "ui"


@dataclass(frozen=True)
class PluginUiAsset:
    """可直接响应给 Web 的插件 UI 静态文件。"""

    path: Path
    media_type: str


class PluginUiHost:
    """在已启用 UI 插件的提取目录内做安全的静态文件解析。"""

    def __init__(self, registry: PluginRegistry, extension_resolver: ExtensionResolver) -> None:
        self._registry = registry
        self._extension_resolver = extension_resolver

    def resolve(self, plugin_id: str, version: str, subpath: str | None) -> PluginUiAsset | None:
        """解析插件 UI 静态文件；不存在或越界返回 None。

        ``subpath`` 为空/None 时回退到 Manifest ``entrypoints.ui`` 声明的默认
        文档；提供了子路径但包含越界分量时直接拒绝（返回 None）。返回前保证文件
        位于该插件的 ``ui/`` 目录内且存在。
        """
        record = self._find(plugin_id, version)
        if record is None:
            return None
        ui_root = self._ui_root(record)
        if ui_root is None:
            return None
        if subpath:
            relative = _safe_ui_relative(subpath)
            if relative is None:
                logger.warning(
                    "插件 UI 路径越界被拒绝: plugin=%s@%s path=%s",
                    plugin_id,
                    version,
                    subpath,
                )
                return None
        else:
            relative = self._entry_relative(record)
            if relative is None:
                return None
        target = (ui_root / relative).resolve()
        if not _is_within(target, ui_root):
            logger.warning("插件 UI 路径越界被拒绝: plugin=%s@%s path=%s", plugin_id, version, relative)
            return None
        if not target.is_file():
            return None
        return PluginUiAsset(path=target, media_type=_media_type(target))

    def _find(self, plugin_id: str, version: str) -> PluginVersionRecord | None:
        try:
            typed_id = PluginId(plugin_id)
            typed_version = SemVer(version)
        except ValueError:
            return None
        return self._registry.get(typed_id, typed_version, PluginPoint.UI)

    def _ui_root(self, record: PluginVersionRecord) -> Path | None:
        entrypoints = record.manifest.entrypoints
        if entrypoints.ui is None or not entrypoints.ui.root.startswith(f"{_UI_DIR}/"):
            return None
        try:
            install_root = self._extension_resolver.install_root(record)
        except Exception:
            logger.exception(
                "插件 UI 提取失败: plugin=%s@%s",
                record.plugin_id.root,
                record.version.root,
            )
            return None
        root = (install_root / _UI_DIR).resolve()
        return root if root.is_dir() else None

    @staticmethod
    def _entry_relative(record: PluginVersionRecord) -> str | None:
        entry = record.manifest.entrypoints.ui
        if entry is None:
            return None
        value = entry.root
        if not value.startswith(f"{_UI_DIR}/"):
            return None
        return value[len(_UI_DIR) + 1 :]


def _safe_ui_relative(subpath: str) -> str | None:
    """把 URL 子路径规范化为 ui/ 内相对路径；含越界分量返回 None。"""
    if subpath.startswith("/") or "\\" in subpath:
        return None
    parts: list[str] = []
    for part in subpath.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts) if parts else None


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _media_type(path: Path) -> str:
    guess, _encoding = mimetypes.guess_type(path.name)
    if guess is None:
        suffix = path.suffix.lower()
        return {
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json",
            ".svg": "image/svg+xml",
            ".woff2": "font/woff2",
        }.get(suffix, "application/octet-stream")
    if guess.startswith("text/") and "charset=" not in guess:
        return f"{guess}; charset=utf-8"
    return guess


__all__ = ["PluginUiAsset", "PluginUiHost"]
