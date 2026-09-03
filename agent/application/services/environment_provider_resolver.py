"""Agent runtime/software 插件入口解析器（一包多 provider、多文件插件）。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypeGuard

from aetp_protocol.discovery import RuntimeProvider, SoftwareProvider
from aetp_protocol.plugin_types import PluginPoint
from aetp_protocol.plugins import PluginManifest

from agent.plugins.registry import PluginRegistry
from common.plugin_loader import load_entrypoint

logger = logging.getLogger(__name__)


class EnvironmentProviderResolver:
    """从已校验插件目录加载 runtime/software 环境发现 Provider。

    一个 runtime/software 插件包可以提供一个或多个 Provider：agent 入口工厂可以
    返回单个 Provider（向后兼容），也可以返回 Provider 的可迭代对象（一包多
    Provider）。每个 Provider 的 ``provider_id`` 必须以插件 ``manifest.id`` 为
    命名空间：等于 ``manifest.id`` 或以 ``manifest.id + "."`` 开头，用于把能力
    归属到其来源插件包。
    """

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry
        self._loaded: dict[str, tuple] = {}

    def runtime_providers(self) -> tuple[RuntimeProvider, ...]:
        """加载所有已安装 runtime 插件的 Provider，单个插件失败不影响其他。"""
        return tuple(self._resolve_point(PluginPoint.RUNTIME, "runtime", _is_runtime_provider))

    def software_providers(self) -> tuple[SoftwareProvider, ...]:
        """加载所有已安装 software 插件的 Provider，单个插件失败不影响其他。"""
        return tuple(self._resolve_point(PluginPoint.SOFTWARE, "software", _is_software_provider))

    def _resolve_point(
        self,
        point: PluginPoint,
        label: str,
        guard: Callable[[object], bool],
    ) -> list:
        providers: list = []
        for installed in self._registry.list():
            try:
                manifest = PluginManifest.model_validate_json(
                    installed.manifest_path.read_text(encoding="utf-8")
                )
                if manifest.point is not point:
                    continue
                providers.extend(
                    self._resolve_plugin(installed.install_path, manifest, label, guard)
                )
            except Exception:
                logger.exception(
                    "%s 插件加载失败: plugin=%s@%s",
                    label,
                    installed.ref.plugin_id.root,
                    installed.ref.version.root,
                )
        return providers

    def _resolve_plugin(
        self,
        install_path: Path,
        manifest: PluginManifest,
        label: str,
        guard: Callable[[object], bool],
    ) -> tuple:
        key = f"{label}:{manifest.id.root}:{manifest.version.root}"
        cached = self._loaded.get(key)
        if cached is not None:
            return cached
        entrypoint = manifest.entrypoints.agent
        if entrypoint is None:
            raise ValueError(f"{label} 插件缺少 agent entrypoint")
        agent_root = (install_path / "agent").resolve()
        if not agent_root.is_dir():
            raise FileNotFoundError(f"{label} 插件缺少 agent 目录: {agent_root}")
        _module, factory = load_entrypoint(agent_root, entrypoint.root)
        providers = self._coerce_providers(factory(), label, guard)
        self._loaded[key] = providers
        return providers

    @staticmethod
    def _coerce_providers(
        value: object,
        label: str,
        guard: Callable[[object], bool],
    ) -> tuple:
        """把工厂返回值规范为 Provider 元组并逐个做类型校验。"""
        if guard(value):
            values: Iterable[object] = (value,)
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
            values = value
        else:
            raise TypeError(f"{label} entrypoint 必须返回 Provider 或 Provider 元组")

        providers: list = []
        seen: set[str] = set()
        for item in values:
            if not guard(item):
                raise TypeError(f"{label} entrypoint 返回值包含非法 Provider")
            provider_id = str(getattr(item, "provider_id", ""))
            if not provider_id.strip():
                raise TypeError(f"{label} Provider 缺少 provider_id")
            if provider_id in seen:
                raise ValueError(f"{label} provider_id 重复: {provider_id}")
            seen.add(provider_id)
            providers.append(item)
        if not providers:
            raise TypeError(f"{label} entrypoint 未返回任何 Provider")
        return tuple(providers)


def _is_runtime_provider(value: object) -> TypeGuard[RuntimeProvider]:
    return (
        value is not None
        and isinstance(getattr(value, "provider_id", None), str)
        and bool(getattr(value, "provider_id", "").strip())
        and isinstance(getattr(value, "runtime_type", None), str)
        and callable(getattr(value, "discover", None))
    )


def _is_software_provider(value: object) -> TypeGuard[SoftwareProvider]:
    return (
        value is not None
        and isinstance(getattr(value, "provider_id", None), str)
        and bool(getattr(value, "provider_id", "").strip())
        and isinstance(getattr(value, "name", None), str)
        and callable(getattr(value, "discover", None))
    )


__all__ = ["EnvironmentProviderResolver"]
