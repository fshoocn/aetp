"""共享任务类型插件的 Master 面接口（P3.8/P5.5）。

Master 面负责把脚本和配置生成可执行任务定义、验证脚本、解析 Case、
计算硬件需求并分割 Shard；Agent 面的执行入口由同一插件包提供，负责
实际执行、日志整合和结果分析。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from aetp_protocol.capabilities import HardwareRequirements
from aetp_protocol.plugin import CaseInfo, ShardSpec, TaskDefinitionSpec


class MasterTaskPlugin(Protocol):
    """Master 侧任务类型插件（纯数据接口，§18.2）。

    具体插件（pytest/cdd/canoe）随包发布，由 bootstrap 容器显式注册到
    PluginRegistry；不得由项目配置上传/下载任意解析代码（§10.4）。
    """

    # sym:task_type 任务类型标识（全局唯一，注册表键）
    task_type: str
    # sym:display_name 展示名（API/前端任务类型清单）
    display_name: str
    # sym:plugin_version 插件版本（Master 校验兼容性）
    plugin_version: str
    # sym:supported_versions 兼容的插件版本集合（旧脚本迁移校验，§18.2）
    supported_versions: frozenset[str]
    # sym:config_schema 配置 Schema（通用表单兜底，D-16）
    config_schema: Mapping[str, Any]
    # sym:upload_spec 上传规格（文件类型白名单/大小上限）
    upload_spec: Mapping[str, Any]

    def verify_script(
        self, script_dir: str, config: Mapping[str, Any]
    ) -> list[str]:
        """脚本编译/格式验证（上传后、解析前调用，快速失败）。

        插件最懂自身脚本格式：
        - pytest：py_compile 语法检查 + collect 兼容预检
        - cdd：XML/格式验证
        - CANoe：工程结构完整性（auto_create/template_upload/complete_upload 三模式）

        返回错误列表（空 = 通过）；有错则脚本 parse_status=failed，
        不进入解析队列（§18.3 步骤 2 前拦截）。
        Master 默认直接调用；需要台架环境时可由 Agent 面提供受控辅助预检，
        但最终可用性判断仍由 Master 工作流完成。
        """
        ...

    async def parse_cases(
        self, script_dir: str, config: Mapping[str, Any]
    ) -> list[CaseInfo]:
        """用例解析：返回 CaseInfo 列表；stable_key 必须稳定（§18.3）。"""
        ...

    def build_task_definition(
        self, config: Mapping[str, Any], cases: list[CaseInfo]
    ) -> TaskDefinitionSpec:
        """根据脚本和配置生成任务定义快照，不创建 Run。"""
        ...

    async def split_shards(
        self,
        cases: list[CaseInfo],
        policy: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> list[ShardSpec]:
        """任务分割：by_time（按耗时切到目标时长）/ by_case_count / custom（§18.6）。"""
        ...

    def result_schema(self, config: Mapping[str, Any]) -> Mapping[str, Any]:
        """声明 Agent 结果分析需要产出的结构，不在 Master 执行报告解析。"""
        ...

    def hardware_requirements(
        self, config: Mapping[str, Any], cases: list[CaseInfo]
    ) -> HardwareRequirements:
        """强类型硬件需求（§18.5 节点匹配依据）。"""
        ...
