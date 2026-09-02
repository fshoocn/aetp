"""Agent V2 resource 插件入口解析器。"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import TypeGuard

from aetp_protocol.plugin_types import PluginPoint
from aetp_protocol.plugins import PluginManifest
from aetp_protocol.resource import ResourceProvider

from agent.plugins.v2_registry import AgentV2PluginRegistry

logger = logging.getLogger(__name__)


class V2ResourceProviderResolver:
    """从已校验 V2 插件目录加载 resource Provider。"""

    def __init__(self, registry: AgentV2PluginRegistry) -> None:
        self._registry = registry
        self._loaded: dict[tuple[str, str], ResourceProvider] = {}

    def resolve_all(self) -> tuple[ResourceProvider, ...]:
        """加载所有已安装 resource 插件，单个插件失败不影响其他 Provider。"""
        providers: list[ResourceProvider] = []
        for installed in self._registry.list():
            try:
                manifest = PluginManifest.model_validate_json(
                    installed.manifest_path.read_text(encoding="utf-8")
                )
                if manifest.point is not PluginPoint.RESOURCE:
                    continue
                providers.append(self.resolve(installed.ref.plugin_id.root, installed.ref.version.root))
            except Exception:
                logger.exception(
                    "V2 resource 插件加载失败: plugin=%s@%s",
                    installed.ref.plugin_id.root,
                    installed.ref.version.root,
                )
        return tuple(providers)

    def resolve(self, plugin_id: str, version: str) -> ResourceProvider:
        """按精确 plugin_id/version 加载 resource Provider。"""
        key = (plugin_id, version)
        existing = self._loaded.get(key)
        if existing is not None:
            return existing
        installed = self._registry.get(plugin_id, version)
        if installed is None:
            raise LookupError(f"V2 resource 插件未安装: {plugin_id}@{version}")
        manifest = PluginManifest.model_validate_json(
            installed.manifest_path.read_text(encoding="utf-8")
        )
        if manifest.point is not PluginPoint.RESOURCE:
            raise ValueError(f"V2 插件不是 resource: {plugin_id}@{version}")
        entrypoint = manifest.entrypoints.agent
        if entrypoint is None:
            raise ValueError("V2 resource 插件缺少 agent entrypoint")
        module_name, attribute_name = entrypoint.root.split(":", 1)
        agent_root = installed.install_path / "agent"
        module_path = (agent_root / (module_name.replace(".", "/") + ".py")).resolve()
        try:
            module_path.relative_to(agent_root.resolve())
        except ValueError as exc:
            raise ValueError("V2 resource entrypoint 越界") from exc
        if not module_path.is_file():
            raise FileNotFoundError(f"V2 resource 入口文件不存在: {module_path}")
        module = self._load_module(module_path, key)
        factory = getattr(module, attribute_name, None)
        if not callable(factory):
            raise TypeError(f"V2 resource entrypoint 不可调用: {entrypoint.root}")
        provider = factory()
        if not _is_resource_provider(provider):
            raise TypeError("V2 resource entrypoint 未返回 ResourceProvider")
        if provider.provider_id != manifest.id.root:
            raise ValueError(
                f"V2 resource provider_id 与 Manifest 不一致: "
                f"{provider.provider_id} != {manifest.id.root}"
            )
        self._loaded[key] = provider
        return provider

    @staticmethod
    def _load_module(path: Path, key: tuple[str, str]) -> ModuleType:
        module_name = f"aetp_v2_resource_{key[0].replace('.', '_')}_{key[1].replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载 V2 resource: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module


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


__all__ = ["V2ResourceProviderResolver"]
