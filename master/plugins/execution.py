"""Agent 侧执行插件（P3.8 增强，§18.2/§9.5）。

插件随 Agent 包分发，Agent 启动时从本地受信任 registry 加载；run.assign
携带 task_type + plugin_version，Agent 按 task_type 查 registry 得到
execute/parse_results 实现。本模块提供：

- ExecutionPlugin：Agent 侧执行插件协议
- ExecutionPluginRegistry：Agent 侧注册表，加载/查询异常显式处理
  （插件缺失 → PLUGIN_NOT_FOUND；版本不兼容 → PLUGIN_VERSION_MISMATCH）

禁止从项目配置上传/下载任意解析代码（§10.4 规则）。
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Protocol

from master.plugins.base import CaseInfo, CaseResult, TaskContext
from master.plugins.capability import PluginCapability
from master.plugins.errors import PluginLoadError, PluginNotFoundError, PluginVersionMismatchError


class ExecutionPlugin(Protocol):
    """Agent 侧执行插件（§18.2）。

    execute 接收 Shard 上下文执行（含 execution_params + case_keys）；
    CANoe 类运行中无 case 级实时数据，结果由 parse_results 解析报告产出（D-19）。
    """

    # sym:task_type 任务类型标识（registry 键，与 run.assign 声明一致）
    task_type: str
    # sym:plugin_version 插件版本（run.assign 校验）
    plugin_version: str
    # sym:supported_versions 兼容版本集合（接收方校验）
    supported_versions: frozenset[str]
    # sym:verify_location 验证执行位置（agent=本端可执行脚本编译/格式验证）
    verify_location: str

    def verify_script(self, script_dir: str, config: dict) -> list[str]:
        """脚本编译/格式验证（verify_location=agent 时在本端执行，结果回传 Master）。

        CANoe 工程验证依赖 CANoe COM，只能在本端执行（§18.3）。
        返回错误列表（空 = 通过）。
        """
        ...

    async def execute(self, context: TaskContext) -> Any:
        """执行一个 Shard（子任务）；只通过 TaskContext 上报进度/日志/结果（§9.5）。"""
        ...

    async def cancel(self) -> None:
        """取消执行（安全点释放硬件）。"""
        ...

    async def parse_cases(
        self, script_dir: str, config: dict
    ) -> list[CaseInfo]:
        """用例解析（仅声明 parse_location=agent 的插件实现，D-17）。"""
        ...

    async def parse_results(
        self, artifact_files: list[str], context: TaskContext
    ) -> list[CaseResult]:
        """测试报告解析（仅声明 result_parse_location=agent 的插件实现，D-19）。"""
        ...


class ExecutionPluginRegistry:
    """Agent 侧执行插件注册表（本地受信任加载）。

    查询与版本校验抛显式异常（§5.5 错误码）：
    - task_type 未注册 → PluginNotFoundError（PLUGIN_NOT_FOUND）
    - 声明版本不在 supported_versions → PluginVersionMismatchError
      （PLUGIN_VERSION_MISMATCH；Agent 应 ACK(rejected)，不推进 Attempt）

    主动上报支持（§9.4/§18.3）：
    - capabilities() 汇总本机插件能力清单（随心跳/变更上报）
    - revision 为清单变更计数：插件注册/变动时递增，供上报方判断
      “监测到变动”是否需要主动上报（node.register/变更消息）
    """

    def __init__(self) -> None:
        self._plugins: dict[str, ExecutionPlugin] = {}
        self._revision: int = 0

    def register(self, plugin: ExecutionPlugin) -> None:
        """注册一个执行插件（task_type 唯一）；注册即视为清单变动。"""
        if plugin.task_type in self._plugins:
            raise ValueError(f"执行插件已注册: {plugin.task_type}")
        self._plugins[plugin.task_type] = plugin
        self._revision += 1

    def discover(self, group: str = "aetp.execution_plugins") -> int:
        """从已安装受信任包的 entry points 自动发现并注册 Agent 侧插件（§10.6）。

        与 uvicorn import_from_string 不同：只加载已安装受信任包声明的
        入口，不从任意配置加载代码（§10.4）。返回新注册数量；
        单个 entry point 加载失败抛 PluginLoadError。
        """
        discovered = entry_points(group=group)
        count = 0
        for ep in discovered:
            try:
                plugin = ep.load()
            except Exception as exc:  # noqa: BLE001 - 加载失败统一转插件加载错误
                raise PluginLoadError(
                    f"执行插件加载失败: {ep.name} ({ep.value}): {exc}"
                ) from exc
            self.register(plugin)
            count += 1
        return count

    @property
    def revision(self) -> int:
        """插件清单变更计数：变化即需主动上报（心跳/变更消息携带）。"""
        return self._revision

    def capabilities(self) -> list[PluginCapability]:
        """本机可执行插件能力清单（AETP_AGENT_SUPPORTED_TASK_TYPES 汇总，§9.4）。

        由 registry 汇总，不信任手工伪造配置；含 plugin_version 与
        supported_versions，供 Master 调度前筛查。
        """
        return [
            PluginCapability(
                task_type=p.task_type,
                plugin_version=p.plugin_version,
                supported_versions=frozenset(p.supported_versions),
                display_name=getattr(p, "display_name", ""),
                parse_location=getattr(p, "parse_location", "master"),
                result_parse_location=getattr(
                    p, "result_parse_location", "master"
                ),
                verify_location=getattr(p, "verify_location", "master"),
            )
            for p in sorted(self._plugins.values(), key=lambda x: x.task_type)
        ]

    def get(self, task_type: str) -> ExecutionPlugin | None:
        """按 task_type 查询（不存在返回 None）。"""
        return self._plugins.get(task_type)

    def require(self, task_type: str) -> ExecutionPlugin:
        """按 task_type 取插件；未安装抛 PluginNotFoundError（PLUGIN_NOT_FOUND）。

        对应场景：run.assign 下发到未安装该插件的 Agent —— Agent
        ACK(rejected, code=PLUGIN_NOT_FOUND)，Master 记录并按策略换节点/失败。
        """
        plugin = self.get(task_type)
        if plugin is None:
            raise PluginNotFoundError(
                f"Agent 未安装任务类型插件: {task_type}"
            )
        return plugin

    def require_compatible(
        self, task_type: str, plugin_version: str
    ) -> ExecutionPlugin:
        """取插件并校验版本兼容（§18.2）。

        对应场景：run.assign 携带的 plugin_version 与本地插件
        supported_versions 不匹配 → ACK(rejected, code=PLUGIN_VERSION_MISMATCH)。
        """
        plugin = self.require(task_type)
        if plugin_version not in plugin.supported_versions:
            raise PluginVersionMismatchError(
                f"Agent 插件版本不兼容: {task_type} 声明 {plugin_version}，"
                f"本地支持 {sorted(plugin.supported_versions)}"
            )
        return plugin

    def supported_task_types(self) -> list[str]:
        """本机可执行的任务类型列表（AETP_AGENT_SUPPORTED_TASK_TYPES 汇总，§9.4）。

        由插件 registry 汇总，不信任手工伪造配置。
        """
        return sorted(self._plugins.keys())
