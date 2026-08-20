"""共享任务类型插件包契约（P5.5）。

一个插件包同时提供两个受信任入口：

* ``master``：任务生成、验证、Case 解析、Shard 分割和硬件需求；
* ``agent``：脚本执行、日志整合、结果分析和进度上报。

本模块只定义包元数据和入口关系，不执行插件逻辑，也不依赖 Master/Agent
运行时实现。两个组件通过同一个 ``aetp.plugins`` entry point 发现同一个包。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .capabilities import HardwareRequirements
from .payloads import PluginPackageRef


@dataclass(frozen=True)
class CaseInfo:
    """Master 插件解析出的稳定用例索引项。"""

    stable_key: str
    name: str
    parent_path: str = ""
    tags: tuple[str, ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)
    estimated_duration_s: float | None = None


@dataclass(frozen=True)
class ShardSpec:
    """Master 插件生成的可调度 Shard。"""

    case_keys: tuple[str, ...]
    execution_params: Mapping[str, Any] = field(default_factory=dict)
    estimated_duration_s: float | None = None


@dataclass(frozen=True)
class TaskDefinitionSpec:
    """Master 插件生成的任务定义快照。"""

    default_case_keys: tuple[str, ...] = ()
    parameter_schema: Mapping[str, Any] = field(default_factory=dict)
    split_policy: Mapping[str, Any] = field(default_factory=dict)
    retry_policy: Mapping[str, Any] = field(default_factory=dict)
    timeout_s: int = 0
    hardware_requirements: HardwareRequirements = field(
        default_factory=HardwareRequirements
    )


class AgentTaskContext(Protocol):
    """Agent 执行上下文；插件不能依赖 MQTT、数据库或 FastAPI。"""

    task_id: str
    shard_id: str
    run_id: str
    node_id: str
    params: Mapping[str, Any]
    script_ref: Mapping[str, Any]

    async def progress(
        self, percent: int, stage: str, message: str = ""
    ) -> None: ...

    async def log(
        self, level: str, message: str, detail: Mapping[str, Any] | None = None
    ) -> None: ...

    async def capture_log(
        self, stream: str, message: str, detail: Mapping[str, Any] | None = None
    ) -> None: ...

    def is_cancelled(self) -> bool: ...

    async def raise_if_cancelled(self) -> None: ...


class AgentExecutionPlugin(Protocol):
    """共享任务类型插件包中的 Agent 执行入口。

    除执行职责外，插件**可选**声明台架侧辅助预检/解析能力（P5.7，D-17）：

    - ``verify_location == "agent"`` 且实现 ``verify_script``：具备台架环境
      下脚本编译/格式预检能力（如 CANoe COM）；
    - ``parse_location == "agent"`` 且实现 ``parse_cases``：具备台架环境下
      解析用例索引的能力。

    两者均属**受控辅助**——Agent 只回传事实，主用例索引与 ``parse_status``
    仍由 Master 校验后决定，不把解析权威转移给 Agent。
    """

    task_type: str
    plugin_version: str
    supported_versions: frozenset[str]
    display_name: str

    # sym:verify_location 脚本验证执行位置（master/agent；默认 master）
    verify_location: str
    # sym:parse_location 用例解析位置（master/agent；默认 master）
    parse_location: str

    async def execute(self, context: AgentTaskContext) -> Any: ...

    async def cancel(self) -> None: ...

    async def analyze_results(
        self,
        execution_result: Any,
        context: AgentTaskContext,
    ) -> Mapping[str, Any]: ...

    async def collect_logs(self, context: AgentTaskContext) -> None: ...

    def verify_script(
        self, script_dir: str, config: Mapping[str, Any]
    ) -> list[str]:
        """（可选）台架侧脚本预检：返回错误列表，空 = 通过。"""
        ...

    def parse_cases(
        self, script_dir: str, config: Mapping[str, Any]
    ) -> list[CaseInfo]:
        """（可选）台架侧用例解析：返回 CaseInfo 事实列表（同步）。"""
        ...


@dataclass(frozen=True)
class AgentPackageSpec:
    """插件包中 Agent 执行入口的可分发元数据。"""

    package_name: str
    version: str
    download_url: str
    sha256: str
    entry_point: str

    def to_ref(self, task_type: str) -> PluginPackageRef:
        return PluginPackageRef(
            task_type=task_type,
            package_name=self.package_name,
            version=self.version,
            download_url=self.download_url,
            sha256=self.sha256,
            entry_point=self.entry_point,
        )


@dataclass(frozen=True)
class PluginMetadata:
    """Master/Agent 共用的任务类型元数据。"""

    task_type: str
    plugin_version: str
    supported_versions: frozenset[str]
    display_name: str = ""
    config_schema: dict[str, Any] = field(default_factory=dict)
    upload_spec: dict[str, Any] = field(default_factory=dict)
    # P7.2：前端专用配置页标识与最低前端版本
    ui: dict[str, Any] = field(default_factory=dict)
    agent_package: AgentPackageSpec | None = None


@dataclass(frozen=True)
class PluginPackage:
    """同一插件包的 Master/Agent 双端入口。"""

    metadata: PluginMetadata
    master: Any
    agent: Any

    def __post_init__(self) -> None:
        agent_package = self.metadata.agent_package
        if agent_package is not None and agent_package.version != self.metadata.plugin_version:
            raise ValueError(
                "Agent 插件包版本必须与共享插件版本一致: "
                f"{agent_package.version} != {self.metadata.plugin_version}"
            )

    def agent_package_ref(self) -> PluginPackageRef | None:
        if self.metadata.agent_package is None:
            return None
        return self.metadata.agent_package.to_ref(self.metadata.task_type)
