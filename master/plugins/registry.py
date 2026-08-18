"""任务类型插件注册表（P3.8，§架构树 plugins/registry.py）。

task_type -> PluginPackage 注册表：
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

from aetp_protocol.payloads import PluginPackageRef
from aetp_protocol.plugin import PluginMetadata, PluginPackage

from master.plugins.base import MasterTaskPlugin
from master.plugins.errors import PluginLoadError, PluginNotFoundError, PluginVersionMismatchError


class PluginRegistry:
    """任务类型插件注册表。

    注册表只依赖共享 PluginPackage 和 MasterTaskPlugin 端口，不 import adapter。
    """

    def __init__(self) -> None:
        self._packages: dict[str, PluginPackage] = {}

    def register(self, package: PluginPackage) -> None:
        """注册一个完整共享插件包；裸 Master 插件不允许注册。"""
        if not isinstance(package, PluginPackage):
            raise TypeError("Master registry 只接受 aetp_protocol.PluginPackage")
        _validate_package_metadata(package, package.master)
        task_type = package.metadata.task_type
        if task_type in self._packages:
            raise ValueError(f"任务类型已注册: {task_type}")
        self._packages[task_type] = package

    def discover(self, group: str = "aetp.plugins", disabled_task_types: set[str] | None = None) -> int:
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
                plugin = _materialize(ep.load())
            except Exception as exc:  # noqa: BLE001 - 加载失败统一转插件加载错误
                raise PluginLoadError(
                    f"插件加载失败: {ep.name} ({ep.value}): {exc}"
                ) from exc
            if plugin.metadata.task_type in (disabled_task_types or set()):
                continue
            self.register(plugin)
            count += 1
        return count

    def get(self, task_type: str) -> PluginPackage | None:
        """按 task_type 查询共享插件包（不存在返回 None）。"""
        return self._packages.get(task_type)

    def require(self, task_type: str) -> PluginPackage:
        """按 task_type 取共享插件包。"""
        package = self.get(task_type)
        if package is None:
            raise PluginNotFoundError(f"任务类型未注册: {task_type}")
        return package

    def require_compatible(self, task_type: str, plugin_version: str) -> PluginPackage:
        """取插件并校验版本兼容（§18.2）。

        - 未注册 → PluginNotFoundError（PLUGIN_NOT_FOUND）
        - plugin_version 不在 supported_versions → PluginVersionMismatchError
          （PLUGIN_VERSION_MISMATCH，Master 派发前拒绝）
        """
        package = self.require(task_type)
        if plugin_version not in package.metadata.supported_versions:
            raise PluginVersionMismatchError(
                f"插件版本不兼容: {task_type} 声明 {plugin_version}，"
                f"支持 {sorted(package.metadata.supported_versions)}"
            )
        return package

    def list(self) -> list[PluginPackage]:
        """按 task_type 排序返回全部共享插件包。"""
        return sorted(self._packages.values(), key=lambda p: p.metadata.task_type)

    def is_version_compatible(self, task_type: str, plugin_version: str) -> bool:
        """插件版本兼容校验（§18.2）：plugin_version 须在 supported_versions 内。"""
        package = self.get(task_type)
        if package is None:
            return False
        return plugin_version in package.metadata.supported_versions

    def metadata(self, task_type: str) -> PluginMetadata:
        """返回任务类型的共享元数据，供 API/调度器使用。"""
        return self.require(task_type).metadata

    def build_task_definition(self, task_type: str, config, cases):
        """由 Master 插件生成任务定义快照，不创建 Run。"""
        return self.require(task_type).master.build_task_definition(config, cases)

    def result_schema(self, task_type: str, config):
        """返回 Agent 结果分析的结构约束。"""
        return self.require(task_type).master.result_schema(config)

    def agent_package_ref(self, task_type: str) -> PluginPackageRef | None:
        """返回插件声明的 Agent 执行包引用；无动态包时返回 None。"""
        package = self.get(task_type)
        if package is None:
            return None
        return package.agent_package_ref()


def create_default_registry(
    disabled_task_types: set[str] | None = None,
    zip_packages: list[PluginPackage] | None = None,
) -> PluginRegistry:
    """创建 Master 默认注册表并加载已安装受信任插件。"""
    registry = PluginRegistry()
    registry.discover(disabled_task_types=disabled_task_types)
    for package in zip_packages or []:
        if package.metadata.task_type in (disabled_task_types or set()):
            continue
        registry.register(package)
    return registry


def _materialize(value):
    """实例化受信任 entry point 返回的插件包/工厂类。"""
    if isinstance(value, type):
        return value()
    return value


def _validate_package_metadata(package: PluginPackage, plugin: MasterTaskPlugin) -> None:
    metadata = package.metadata
    if (
        metadata.task_type != plugin.task_type
        or metadata.plugin_version != plugin.plugin_version
    ):
        raise ValueError(
            "共享插件包 metadata 与 Master 入口不一致: "
            f"{metadata.task_type}@{metadata.plugin_version} != "
            f"{plugin.task_type}@{plugin.plugin_version}"
        )
