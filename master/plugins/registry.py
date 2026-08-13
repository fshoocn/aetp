"""任务类型插件注册表（P3.8，§架构树 plugins/registry.py）。

task_type -> TaskTypePlugin 注册表：
- 插件由 bootstrap 容器显式注册（受信任随包代码，§10.6）
- 按 task_type 查询插件元数据与数据接口
- 版本兼容校验（§18.2）：插件升级后需声明 supported_versions 才能
  解析/执行旧脚本，否则 Master 提示 PLUGIN_VERSION_MISMATCH
- require/require_compatible 提供显式异常：插件缺失或版本不兼容时
  抛出 PluginError（§5.5 错误码），由 API/派发层映射
- discover() 从已安装受信任包的 entry points 自动发现（§10.6）：
  与 uvicorn import_from_string（任意配置字符串加载）不同，发现范围
  限定于随包分发的受信任扩展，不执行配置/数据库中的任意代码（§10.4）
"""

from __future__ import annotations

from importlib.metadata import entry_points

from master.plugins.base import TaskTypePlugin
from master.plugins.errors import PluginLoadError, PluginNotFoundError, PluginVersionMismatchError


class PluginRegistry:
    """任务类型插件注册表。

    注册表只依赖 TaskTypePlugin 端口（数据耦合），不 import 任何 adapter。
    """

    def __init__(self) -> None:
        self._plugins: dict[str, TaskTypePlugin] = {}

    def register(self, plugin: TaskTypePlugin) -> None:
        """注册一个插件（task_type 唯一）。"""
        if plugin.task_type in self._plugins:
            raise ValueError(f"任务类型已注册: {plugin.task_type}")
        self._plugins[plugin.task_type] = plugin

    def discover(self, group: str = "aetp.plugins") -> int:
        """从已安装受信任包的 entry points 自动发现并注册 Master 侧插件（§10.6）。

        插件包在安装元数据中声明 entry point（如
        ``aetp.plugins = "can_test = aetp_plugins.can:CanTestPlugin"``），
        bootstrap 启动时调用本方法即可发现，无需改容器代码。

        与 uvicorn import_from_string 不同：只加载**已安装的受信任包**
        声明的入口，不从任意配置/数据库字符串加载代码（§10.4）。

        返回新注册数量；单个 entry point 加载失败抛 PluginLoadError。
        """
        discovered = entry_points(group=group)
        count = 0
        for ep in discovered:
            try:
                plugin = ep.load()
            except Exception as exc:  # noqa: BLE001 - 加载失败统一转插件加载错误
                raise PluginLoadError(
                    f"插件加载失败: {ep.name} ({ep.value}): {exc}"
                ) from exc
            self.register(plugin)
            count += 1
        return count

    def get(self, task_type: str) -> TaskTypePlugin | None:
        """按 task_type 查询插件（不存在返回 None）。"""
        return self._plugins.get(task_type)

    def require(self, task_type: str) -> TaskTypePlugin:
        """按 task_type 取插件；未注册抛 PluginNotFoundError（PLUGIN_NOT_FOUND）。"""
        plugin = self.get(task_type)
        if plugin is None:
            raise PluginNotFoundError(f"任务类型未注册: {task_type}")
        return plugin

    def require_compatible(self, task_type: str, plugin_version: str) -> TaskTypePlugin:
        """取插件并校验版本兼容（§18.2）。

        - 未注册 → PluginNotFoundError（PLUGIN_NOT_FOUND）
        - plugin_version 不在 supported_versions → PluginVersionMismatchError
          （PLUGIN_VERSION_MISMATCH，Master 派发前拒绝）
        """
        plugin = self.require(task_type)
        if plugin_version not in plugin.supported_versions:
            raise PluginVersionMismatchError(
                f"插件版本不兼容: {task_type} 声明 {plugin_version}，"
                f"支持 {sorted(plugin.supported_versions)}"
            )
        return plugin

    def list(self) -> list[TaskTypePlugin]:
        """按 task_type 排序返回全部插件（API 任务类型清单数据源）。"""
        return sorted(self._plugins.values(), key=lambda p: p.task_type)

    def is_version_compatible(self, task_type: str, plugin_version: str) -> bool:
        """插件版本兼容校验（§18.2）：plugin_version 须在 supported_versions 内。"""
        plugin = self.get(task_type)
        if plugin is None:
            return False
        return plugin_version in plugin.supported_versions
