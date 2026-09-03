"""Agent resource 插件入口解析器（支持一包多 provider、多文件插件）。"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import TypeGuard

from aetp_protocol.plugin_types import PluginPoint
from aetp_protocol.plugins import PluginManifest
from aetp_protocol.resource import ResourceProvider

from agent.plugins.loader import load_entrypoint
from agent.plugins.registry import PluginRegistry

logger = logging.getLogger(__name__)


class ResourceProviderResolver:
    """从已校验插件目录加载 resource Provider。

    一个 resource 插件包可以提供一个或多个 Provider：agent 入口工厂可以返回单个
    ResourceProvider（向后兼容），也可以返回 ResourceProvider 的可迭代对象（一包多
    Provider，例如一个包同时提供 serial/power/can 三种资源能力）。每个 Provider 的
    ``provider_id`` 必须以插件 ``manifest.id`` 为命名空间：等于 ``manifest.id`` 或
    以 ``manifest.id + "."`` 开头，用于把 Provider 归属到其来源插件包。
    """

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry
        self._loaded: dict[tuple[str, str], tuple[ResourceProvider, ...]] = {}

    def resolve_all(self) -> tuple[ResourceProvider, ...]:
        """加载所有已安装 resource 插件产出的 Provider，单个插件失败不影响其他。"""
        providers: list[ResourceProvider] = []
        for installed in self._registry.list():
            try:
                manifest = PluginManifest.model_validate_json(
                    installed.manifest_path.read_text(encoding="utf-8")
                )
                if manifest.point is not PluginPoint.RESOURCE:
                    continue
                providers.extend(
                    self._resolve_plugin(installed.install_path, manifest)
                )
            except Exception:
                logger.exception(
                    " resource 插件加载失败: plugin=%s@%s",
                    installed.ref.plugin_id.root,
                    installed.ref.version.root,
                )
        return tuple(providers)

    def resolve(self, plugin_id: str, version: str) -> ResourceProvider:
        """按精确 plugin_id/version 加载该 resource 插件的首个 Provider。

        兼容旧调用：单 Provider 包返回其唯一 Provider。多 Provider 包请使用
        ``resolve_all()`` 获得全部 Provider。
        """
        key = (plugin_id, version)
        cached = self._loaded.get(key)
        if cached is not None:
            return cached[0]
        installed = self._registry.get(plugin_id, version)
        if installed is None:
            raise LookupError(f" resource 插件未安装: {plugin_id}@{version}")
        manifest = PluginManifest.model_validate_json(
            installed.manifest_path.read_text(encoding="utf-8")
        )
        if manifest.point is not PluginPoint.RESOURCE:
            raise ValueError(f" 插件不是 resource: {plugin_id}@{version}")
        providers = self._resolve_plugin(installed.install_path, manifest)
        if not providers:
            raise ValueError(f" resource 插件未产出 Provider: {plugin_id}@{version}")
        return providers[0]

    def _resolve_plugin(
        self,
        install_path: Path,
        manifest: PluginManifest,
    ) -> tuple[ResourceProvider, ...]:
        key = (manifest.id.root, manifest.version.root)
        cached = self._loaded.get(key)
        if cached is not None:
            return cached
        entrypoint = manifest.entrypoints.agent
        if entrypoint is None:
            raise ValueError(" resource 插件缺少 agent entrypoint")
        agent_root = (install_path / "agent").resolve()
        if not agent_root.is_dir():
            raise FileNotFoundError(f" resource 插件缺少 agent 目录: {agent_root}")
        _module, factory = load_entrypoint(agent_root, entrypoint.root)
        providers = self._coerce_providers(factory(), manifest)
        if not providers:
            raise TypeError(" resource entrypoint 未返回任何 ResourceProvider")
        self._loaded[key] = providers
        return providers

    @staticmethod
    def _coerce_providers(value: object, manifest: PluginManifest) -> tuple[ResourceProvider, ...]:
        """把工厂返回值规范为 Provider 元组并逐个做类型校验。

        一个 resource 插件包可以承载多个真实来源的 Provider（例如同一个包同时提供
        ``org.aetp.serial-resource`` 与 ``com.vector.can-resource``）。每个
        Provider 的 ``provider_id`` 是其资源归属与路由标识，不必等于插件
        ``manifest.id``；``manifest.id`` 只标识插件包本身。因此这里只做
        ResourceProvider 类型校验，不强制 provider_id 与 manifest.id 绑定。
        """
        if _is_resource_provider(value):
            values: Iterable[object] = (value,)
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
            values = value
        else:
            raise TypeError(" resource entrypoint 必须返回 ResourceProvider 或 Provider 元组")

        providers: list[ResourceProvider] = []
        seen: set[str] = set()
        for item in values:
            if not _is_resource_provider(item):
                raise TypeError(" resource entrypoint 返回值包含非 ResourceProvider")
            pid = item.provider_id
            if pid in seen:
                raise ValueError(f" resource provider_id 重复: {pid}")
            seen.add(pid)
            providers.append(item)
        return tuple(providers)


def _is_resource_provider(value: object) -> TypeGuard[ResourceProvider]:
    return (
        value is not None
        and isinstance(getattr(value, "provider_id", None), str)
        and bool(getattr(value, "provider_id", "").strip())
        and isinstance(getattr(value, "resource_type", None), str)
        and callable(getattr(value, "discover", None))
        and callable(getattr(value, "activate", None))
        and callable(getattr(value, "deactivate", None))
    )


__all__ = ["ResourceProviderResolver"]
