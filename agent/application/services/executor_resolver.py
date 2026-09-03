"""Agent  executor entrypoint 解析器（统一经 common.plugin_loader 加载）。"""

from __future__ import annotations

from aetp_protocol.execution import ExecutionPlan
from aetp_protocol.plugin_types import PluginPoint
from aetp_protocol.plugins import PluginManifest

from agent.plugins.registry import PluginRegistry
from common.plugin_loader import load_entrypoint


class ExecutorResolver:
    """从已校验  插件目录加载精确 executor 版本。"""

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry
        self._loaded: dict[tuple[str, str], object] = {}

    def resolve(self, plan: ExecutionPlan) -> object:
        """按 Plan 的 plugin_id/version 加载 Agent executor。"""
        key = (plan.executor.plugin_id.root, plan.executor.version.root)
        existing = self._loaded.get(key)
        if existing is not None:
            return existing
        installed = self._registry.get(*key)
        if installed is None:
            raise LookupError(f" executor 未安装: {key[0]}@{key[1]}")
        manifest = PluginManifest.model_validate_json(installed.manifest_path.read_text(encoding="utf-8"))
        if manifest.point is not PluginPoint.EXECUTOR:
            raise ValueError(f" 插件不是 executor: {key[0]}@{key[1]}")
        entrypoint = manifest.entrypoints.agent
        if entrypoint is None:
            raise ValueError(" executor 缺少 agent entrypoint")
        agent_root = (installed.install_path / "agent").resolve()
        if not agent_root.is_dir():
            raise FileNotFoundError(f" executor 插件缺少 agent 目录: {agent_root}")
        _module, factory = load_entrypoint(agent_root, entrypoint.root)
        executor = factory()
        if executor is None or not callable(getattr(executor, "execute", None)):
            raise TypeError(" executor entrypoint 未返回可执行对象")
        version = getattr(executor, "plugin_version", None)
        if version is not None and version != plan.executor.version.root:
            raise ValueError(" executor 版本与 Plan 不一致")
        self._loaded[key] = executor
        return executor


__all__ = ["ExecutorResolver"]
