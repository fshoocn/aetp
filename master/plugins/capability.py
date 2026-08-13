"""插件能力上报与 Master 端筛查（P3.8 增强，§9.4/§18.3）。

插件能力像硬件能力一样**主动上报**：
- 随心跳（node.heartbeat）按频率周期性上报；
- 监测到插件清单变动（registry revision 变化）时主动上报（node.register/变更）。
Master 端在**调度前**用能力清单筛查节点（§18.5：能力满足 > 负载最低 >
最近在线），避免下发 run.assign 时才被动发现 PLUGIN_NOT_FOUND /
PLUGIN_VERSION_MISMATCH。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class PluginCapability:
    """Agent 上报的插件能力条目（node.register / 变更上报 / 心跳携带载荷）。

    由 Agent 端 ExecutionPluginRegistry.capabilities() 汇总生成，
    不信任手工伪造配置（§9.4）。
    """

    # sym:task_type 任务类型标识（可执行/可解析）
    task_type: str
    # sym:plugin_version 当前插件版本
    plugin_version: str
    # sym:supported_versions 兼容版本集合（Master 校验旧脚本/下发版本）
    supported_versions: frozenset[str]
    # sym:display_name 插件展示名（可选，Agent 插件未声明时为空）
    display_name: str = ""
    # sym:parse_location 用例解析位置（master/agent，D-17；parse_capabilities 依据）
    parse_location: str = "master"
    # sym:result_parse_location 报告解析位置（master/agent，D-19）
    result_parse_location: str = "master"
    # sym:verify_location 验证执行位置（agent=具备脚本编译/格式验证能力，verify_capabilities）
    verify_location: str = "master"

    def supports(self, plugin_version: str | None = None) -> bool:
        """本能力是否兼容指定插件版本（None = 任意版本均支持本 task_type）。"""
        if plugin_version is None:
            return True
        return plugin_version in self.supported_versions


def filter_supported(
    capabilities: Iterable[PluginCapability],
    task_type: str,
    plugin_version: str | None = None,
) -> list[PluginCapability]:
    """Master 端筛查：从候选 Agent 能力清单中筛出支持指定任务类型的条目。

    - plugin_version 为空：只按 task_type 过滤（取在线节点后由调度器排序）
    - plugin_version 指定：同时校验版本兼容（防 PLUGIN_VERSION_MISMATCH）
    """
    return [
        c
        for c in capabilities
        if c.task_type == task_type and c.supports(plugin_version)
    ]
