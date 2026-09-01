"""Agent V2 executor entrypoint 解析器。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from aetp_protocol.execution import ExecutionPlan
from aetp_protocol.plugin_types import PluginPoint
from aetp_protocol.plugins import PluginManifest

from agent.plugins.v2_registry import AgentV2PluginRegistry


class V2ExecutorResolver:
    """从已校验 V2 插件目录加载精确 executor 版本。"""

    def __init__(self, registry: AgentV2PluginRegistry) -> None:
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
            raise LookupError(f"V2 executor 未安装: {key[0]}@{key[1]}")
        manifest = PluginManifest.model_validate_json(installed.manifest_path.read_text(encoding="utf-8"))
        if manifest.point is not PluginPoint.EXECUTOR:
            raise ValueError(f"V2 插件不是 executor: {key[0]}@{key[1]}")
        entrypoint = manifest.entrypoints.agent
        if entrypoint is None:
            raise ValueError("V2 executor 缺少 agent entrypoint")
        module_name, attribute_name = entrypoint.root.split(":", 1)
        agent_root = installed.install_path / "agent"
        module_path = (agent_root / (module_name.replace(".", "/") + ".py")).resolve()
        try:
            module_path.relative_to(agent_root.resolve())
        except ValueError as exc:
            raise ValueError("V2 executor entrypoint 越界") from exc
        module = self._load_module(module_path, key)
        factory = getattr(module, attribute_name, None)
        if not callable(factory):
            raise TypeError(f"V2 executor entrypoint 不可调用: {entrypoint.root}")
        executor = factory()
        if executor is None or not callable(getattr(executor, "execute", None)):
            raise TypeError("V2 executor entrypoint 未返回可执行对象")
        version = getattr(executor, "plugin_version", None)
        if version is not None and version != plan.executor.version.root:
            raise ValueError("V2 executor 版本与 Plan 不一致")
        self._loaded[key] = executor
        return executor

    @staticmethod
    def _load_module(path: Path, key: tuple[str, str]) -> ModuleType:
        module_name = f"aetp_v2_executor_{key[0].replace('.', '_')}_{key[1].replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载 V2 executor: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module


__all__ = ["V2ExecutorResolver"]
