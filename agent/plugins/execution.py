"""Agent 侧执行插件协议与注册表（P5.5）。

Agent 插件不负责脚本解析、节点能力匹配或脚本验证；这些职责属于 Master
侧 ``MasterTaskPlugin``。Agent 插件只负责执行已分片的任务、采集/整合日志、
分析执行结果，并通过 ``AgentTaskContext`` 回调进度与日志。
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points

from aetp_protocol.plugin import (
    AgentExecutionPlugin,
    AgentTaskContext,  # noqa: F401 - 供 agent.plugins.__init__ re-export
    PluginPackage,
)

from agent.plugins.errors import (
    PluginLoadError,
    PluginNotFoundError,
    PluginVersionMismatchError,
)


@dataclass(frozen=True)
class AgentPluginCapability:
    """Agent 上报给 Master 的执行能力。"""

    task_type: str
    plugin_version: str
    supported_versions: frozenset[str]
    display_name: str = ""
    # sym:verify_location 脚本验证执行位置（agent=具备台架侧预检能力，P5.7）
    verify_location: str = "master"
    # sym:parse_location 用例解析位置（agent=具备台架侧解析能力，P5.7）
    parse_location: str = "master"

    def supports(self, version: str) -> bool:
        return version in self.supported_versions


class AgentPluginRegistry:
    """Agent 本地执行插件注册表。"""

    def __init__(self) -> None:
        self._plugins: dict[str, AgentExecutionPlugin] = {}
        self._revision = 0

    def register_package(
        self, package: PluginPackage, *, replace: bool = False
    ) -> None:
        """注册共享插件包的 Agent 面。"""
        if not isinstance(package, PluginPackage):
            raise TypeError("Agent registry 只接受 aetp_protocol.PluginPackage")
        plugin = package.agent
        if (
            package.metadata.task_type != plugin.task_type
            or package.metadata.plugin_version != plugin.plugin_version
        ):
            raise ValueError("共享插件包 metadata 与 Agent 入口不一致")
        self.register_installed(plugin, replace=replace)

    def register_installed(
        self, plugin: AgentExecutionPlugin, *, replace: bool = False
    ) -> None:
        """注册已通过 SHA-256/入口校验的 Agent 执行入口。"""
        if plugin.task_type in self._plugins and not replace:
            raise ValueError(f"执行插件已注册: {plugin.task_type}")
        self._plugins[plugin.task_type] = plugin
        self._revision += 1

    @property
    def revision(self) -> int:
        return self._revision

    def get(self, task_type: str) -> AgentExecutionPlugin | None:
        return self._plugins.get(task_type)

    def discover(self, group: str = "aetp.plugins") -> int:
        """从同一共享插件 entry point 发现并注册 Agent 面。"""
        discovered = entry_points(group=group)
        count = 0
        for entry_point in discovered:
            try:
                loaded = entry_point.load()
                package = _materialize(loaded)
                if not isinstance(package, PluginPackage):
                    raise TypeError(
                        "aetp.plugins entry point 必须返回 PluginPackage"
                    )
                self.register_package(package, replace=True)
            except Exception as exc:  # noqa: BLE001 - entry point 边界统一映射
                raise PluginLoadError(
                    f"Agent 插件加载失败: {entry_point.name}: {exc}"
                ) from exc
            count += 1
        return count

    def require(self, task_type: str) -> AgentExecutionPlugin:
        plugin = self.get(task_type)
        if plugin is None:
            raise PluginNotFoundError(f"Agent 未安装任务类型插件: {task_type}")
        return plugin

    def require_compatible(
        self, task_type: str, plugin_version: str
    ) -> AgentExecutionPlugin:
        plugin = self.require(task_type)
        if plugin_version not in plugin.supported_versions:
            raise PluginVersionMismatchError(
                f"Agent 插件版本不兼容: {task_type} 声明 {plugin_version}，"
                f"本地支持 {sorted(plugin.supported_versions)}"
            )
        return plugin

    def ensure_compatible(
        self,
        task_type: str,
        plugin_version: str,
        *,
        package_ref=None,
        installer=None,
    ) -> AgentExecutionPlugin:
        """已有兼容插件直接复用，否则通过 installer 安装后再校验。"""
        try:
            return self.require_compatible(task_type, plugin_version)
        except (PluginNotFoundError, PluginVersionMismatchError):
            if package_ref is None or installer is None:
                raise
            if (
                package_ref.task_type != task_type
            ):
                raise PluginVersionMismatchError(
                    "run.assign.plugin_ref 与 task_type 不一致"
                )
            plugin = installer.install(package_ref)
            self.register_installed(plugin, replace=True)
            return self.require_compatible(task_type, plugin_version)

    def capabilities(self) -> list[AgentPluginCapability]:
        return [
            AgentPluginCapability(
                task_type=plugin.task_type,
                plugin_version=plugin.plugin_version,
                supported_versions=frozenset(plugin.supported_versions),
                display_name=getattr(plugin, "display_name", ""),
                verify_location=getattr(plugin, "verify_location", "master"),
                parse_location=getattr(plugin, "parse_location", "master"),
            )
            for plugin in sorted(
                self._plugins.values(), key=lambda item: item.task_type
            )
        ]

    def supported_task_types(self) -> list[str]:
        return sorted(self._plugins)


def _materialize(value):
    if isinstance(value, type):
        return value()
    return value
