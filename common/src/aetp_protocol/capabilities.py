"""节点能力与硬件需求公共模型（P4.5 重构，§18.5）。

能力和需求都使用强类型对象图，不使用动态 ``dict[str, Any]``：

    NodeCapabilities
      ├── VehicleCapability
      │     └── VehicleVendor -> VehicleBus -> HardwareChannel
      ├── LanguageCapability -> LanguageRuntime
      ├── SystemCapability -> OperatingSystem
      └── SerialCapability -> SerialPortCapability

任务需求使用对应的 Requirement 类表达，不再解释 ``cap/op/kind`` 字符串：

    HardwareRequirements
      ├── VehicleRequirement -> BusRequirement
      ├── LanguageRequirement
      ├── SystemRequirement
      └── SerialPortRequirement

厂商、总线类型、语言名、串口功能是开放的字符串值；结构和叶子类型由
Pydantic 模型固定。新增 Vector、同星、LIN、ETH、示波器等只新增对象数据，
不修改通用匹配逻辑。新增能力类别时才新增一组模型和对应 matcher。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from .errors import ErrorCode
from .ids import BusinessId, CapabilityName, JsonObject, PluginId, SemVer, SessionId, Sha256
from .plugin_types import PluginAvailability, PluginPoint


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Identifier = Annotated[str, Field(min_length=1, max_length=128)]


class Version(RootModel[str]):
    """可比较的点分数字版本（例如 17.10、3.11、10.0.19045）。"""

    root: str = Field(min_length=1, pattern=r"^v?\d+(?:\.\d+)*$")


class NumericConstraint(_Strict):
    """数值约束；至少设置 exact/minimum/maximum 之一。"""

    exact: float | None = None
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def _validate_bounds(self) -> NumericConstraint:
        if self.exact is None and self.minimum is None and self.maximum is None:
            raise ValueError("数值约束至少需要 exact/minimum/maximum 之一")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("数值约束 minimum 不能大于 maximum")
        if self.exact is not None:
            if self.minimum is not None and self.exact < self.minimum:
                raise ValueError("数值约束 exact 不能小于 minimum")
            if self.maximum is not None and self.exact > self.maximum:
                raise ValueError("数值约束 exact 不能大于 maximum")
        return self


class VersionConstraint(_Strict):
    """语义化版本约束；比较由 Master 的 VersionMatcher 负责。"""

    exact: Version | None = None
    minimum: Version | None = None
    maximum: Version | None = None

    @model_validator(mode="after")
    def _validate_presence(self) -> VersionConstraint:
        if self.exact is None and self.minimum is None and self.maximum is None:
            raise ValueError("版本约束至少需要 exact/minimum/maximum 之一")
        return self


class HardwareChannel(_Strict):
    """一个可被分配的车载硬件通道。"""

    name: Identifier
    hardware_model: Identifier | None = None
    enabled: bool = True


class VehicleBus(_Strict):
    """一个厂商下的一类总线（CAN/LIN/ETH 等）。"""

    bus_type: Identifier
    channels: tuple[HardwareChannel, ...] = ()

    @model_validator(mode="after")
    def _validate_unique_channels(self) -> VehicleBus:
        names = [channel.name for channel in self.channels]
        if len(names) != len(set(names)):
            raise ValueError(f"总线 {self.bus_type} 的通道名称不能重复")
        return self


class VehicleVendor(_Strict):
    """车载硬件厂商（Vector、同星等）。"""

    name: Identifier
    buses: tuple[VehicleBus, ...] = ()

    @model_validator(mode="after")
    def _validate_unique_buses(self) -> VehicleVendor:
        names = [bus.bus_type for bus in self.buses]
        if len(names) != len(set(names)):
            raise ValueError(f"厂商 {self.name} 的总线类型不能重复")
        return self


class VehicleCapability(_Strict):
    """总类 vehicle：厂商 -> 总线 -> 硬件通道。"""

    vendors: tuple[VehicleVendor, ...] = ()

    @model_validator(mode="after")
    def _validate_unique_vendors(self) -> VehicleCapability:
        names = [vendor.name for vendor in self.vendors]
        if len(names) != len(set(names)):
            raise ValueError("vehicle 能力中的厂商名称不能重复")
        return self


class LanguageRuntime(_Strict):
    """一个语言运行时（python/java 等）及其版本。"""

    name: Identifier
    version: Version


class LanguageCapability(_Strict):
    """总类 language：语言运行时列表。"""

    runtimes: tuple[LanguageRuntime, ...] = ()


class OperatingSystem(_Strict):
    """操作系统名称与语义化版本。"""

    name: Identifier
    version: Version


class SystemCapability(_Strict):
    """总类 system：操作系统、内存、CPU。"""

    operating_system: OperatingSystem | None = None
    memory_mb: int | None = Field(default=None, ge=0)
    cpu_cores: int | None = Field(default=None, ge=0)


class SerialPortCapability(_Strict):
    """一个串口功能映射（功能名 -> 端口号）。"""

    function: Identifier
    port: Identifier
    enabled: bool = True


class SerialCapability(_Strict):
    """总类 serial：用户预配置的功能 -> 串口号映射。"""

    ports: tuple[SerialPortCapability, ...] = ()


class SwitchPort(_Strict):
    """切换开关的一个端口，带接线标签。"""

    port: Identifier
    labels: dict[str, str] = Field(default_factory=dict)


class SwitchConnection(_Strict):
    """资源经切换开关连接时的路径描述。"""

    switch_device_id: Identifier
    ports: tuple[SwitchPort, ...] = ()


class PhysicalDeviceCapability(_Strict):
    """一个可被 Master 分配的物理资源能力描述。

    ``labels`` 是用户自定义标签，描述“该资源接的是什么/用在哪”，
    例如 ``{"project": "P3", "dut": "ECU-x"}``。

    ``connection`` 非空表示该资源经切换开关接入，各端口有自己的接线标签。
    """

    resource_type: Identifier
    vendor: Identifier | None = None
    model: Identifier | None = None
    channel: Identifier | None = None
    function: Identifier | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    connection: SwitchConnection | None = None


class DeviceRequirement(_Strict):
    """脚本一次执行需要占用的一组物理资源。

    ``required_labels`` 是硬约束（must）：设备标签必须逐项满足；
    ``preferred_labels`` 是软约束（prefer）：命中越多越优先。
    """

    resource_type: Identifier
    quantity: int = Field(default=1, ge=1)
    vendor: Identifier | None = None
    model: Identifier | None = None
    channel: Identifier | None = None
    function: Identifier | None = None
    device_ids: tuple[Identifier, ...] = ()
    required_labels: dict[str, str] = Field(default_factory=dict)
    preferred_labels: dict[str, str] = Field(default_factory=dict)
    allow_switching: bool = False

    @model_validator(mode="after")
    def _validate_device_ids(self) -> DeviceRequirement:
        if len(self.device_ids) > self.quantity:
            raise ValueError("device_ids 数量不能大于 quantity")
        if len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError("device_ids 不能重复")
        return self


class SwitchRouteAllocation(_Strict):
    """经切换开关的分配路径（Agent 据此切换）。"""

    switch_device_id: Identifier
    port: Identifier


class DeviceAllocation(_Strict):
    """Master 为一次 Attempt 实际分配的物理资源（含标签与切换路径回传）。"""

    device_id: Identifier
    resource_type: Identifier
    labels: dict[str, str] = Field(default_factory=dict)
    switch_route: SwitchRouteAllocation | None = None


class NodeCapabilities(_Strict):
    """节点能力总类；所有分类都是明确的公共类。"""

    vehicle: VehicleCapability | None = None
    language: LanguageCapability | None = None
    system: SystemCapability | None = None
    serial: SerialCapability | None = None


class BusRequirement(_Strict):
    """任务对一类车载总线的需求。"""

    bus_type: Identifier
    vendor: Identifier | None = None
    hardware_model: Identifier | None = None
    minimum_channels: int | None = Field(default=None, ge=0)
    required_channels: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _validate_requirement(self) -> BusRequirement:
        if self.minimum_channels is None and not self.required_channels:
            raise ValueError("总线需求至少需要 minimum_channels 或 required_channels")
        return self


class VehicleRequirement(_Strict):
    """任务对车载硬件的需求。

    ``all_of`` 表示全部必须满足，``any_of`` 表示至少一个满足；
    例如 Vector CAN 或同星 CAN 二选一用 ``any_of`` 表达。
    """

    all_of: tuple[BusRequirement, ...] = ()
    any_of: tuple[BusRequirement, ...] = ()

    @model_validator(mode="after")
    def _validate_requirement(self) -> VehicleRequirement:
        if not self.all_of and not self.any_of:
            raise ValueError("车载需求至少需要 all_of 或 any_of 之一")
        return self


class LanguageRequirement(_Strict):
    """任务对语言运行时的需求。"""

    name: Identifier
    version: VersionConstraint | None = None


class OperatingSystemRequirement(_Strict):
    """任务对操作系统的需求。"""

    name: Identifier | None = None
    version: VersionConstraint | None = None


class SystemRequirement(_Strict):
    """任务对系统资源的需求。"""

    operating_system: OperatingSystemRequirement | None = None
    memory_mb: NumericConstraint | None = None
    cpu_cores: NumericConstraint | None = None


class SerialPortRequirement(_Strict):
    """任务对串口功能的需求；按功能找端口，可选指定端口。"""

    function: Identifier
    port: Identifier | None = None
    enabled: bool = True


class HardwareRequirements(_Strict):
    """任务硬件需求总类；各类别由对应 matcher 处理。"""

    vehicle: VehicleRequirement | None = None
    languages: tuple[LanguageRequirement, ...] = ()
    system: SystemRequirement | None = None
    serial_ports: tuple[SerialPortRequirement, ...] = ()
    required_tags: tuple[Identifier, ...] = ()
    devices: tuple[DeviceRequirement, ...] = ()


class ResourceHealth(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class AgentMaintenanceState(StrEnum):
    READY = "ready"
    IDLE = "idle"
    BUSY = "busy"
    DRAINING = "draining"
    UPDATING = "updating"
    RESTARTING = "restarting"
    DEGRADED = "degraded"


class RuntimeCapability(_Strict):
    provider_id: str
    runtime_id: str
    runtime_type: str
    version: Version
    executable_ref: str | None = None


class ExecutorCapability(_Strict):
    plugin_id: PluginId
    version: SemVer
    capabilities: tuple[CapabilityName, ...]


class SoftwareCapability(_Strict):
    provider_id: str
    name: str
    version: Version
    properties: JsonObject = Field(default_factory=dict)


class ResourceCapability(_Strict):
    resource_id: BusinessId
    provider_id: str
    resource_type: str
    vendor: str | None = None
    model: str | None = None
    channel: str | None = None
    function: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    properties: JsonObject = Field(default_factory=dict)
    health: ResourceHealth
    switch_connection: SwitchConnection | None = None


class PluginInventoryItem(_Strict):
    plugin_id: PluginId
    point: PluginPoint
    version: SemVer
    archive_sha256: Sha256
    availability: PluginAvailability
    unavailable_reasons: tuple[ErrorCode, ...] = ()
    checked_at: datetime


class NodeCapabilitySnapshot(_Strict):
    schema_version: Literal[2]
    node_id: BusinessId
    session_id: SessionId
    revision: int = Field(ge=1)
    reported_at: datetime
    tags: tuple[str, ...] = ()
    executors: tuple[ExecutorCapability, ...] = ()
    runtimes: tuple[RuntimeCapability, ...] = ()
    software: tuple[SoftwareCapability, ...] = ()
    resources: tuple[ResourceCapability, ...] = ()
    system: SystemCapability | None = None
    maintenance_state: AgentMaintenanceState
    plugin_inventory: tuple[PluginInventoryItem, ...] = ()


PluginInventoryItem.model_rebuild()
NodeCapabilitySnapshot.model_rebuild()
