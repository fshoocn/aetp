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

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator


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
    def _validate_bounds(self) -> "NumericConstraint":
        if self.exact is None and self.minimum is None and self.maximum is None:
            raise ValueError("数值约束至少需要 exact/minimum/maximum 之一")
        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
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
    def _validate_presence(self) -> "VersionConstraint":
        if self.exact is None and self.minimum is None and self.maximum is None:
            raise ValueError("版本约束至少需要 exact/minimum/maximum 之一")
        return self


class HardwareChannel(_Strict):
    """一个可被分配的车载硬件通道。"""

    name: Identifier
    enabled: bool = True


class VehicleBus(_Strict):
    """一个厂商下的一类总线（CAN/LIN/ETH 等）。"""

    bus_type: Identifier
    channels: tuple[HardwareChannel, ...] = ()

    @model_validator(mode="after")
    def _validate_unique_channels(self) -> "VehicleBus":
        names = [channel.name for channel in self.channels]
        if len(names) != len(set(names)):
            raise ValueError(f"总线 {self.bus_type} 的通道名称不能重复")
        return self


class VehicleVendor(_Strict):
    """车载硬件厂商（Vector、同星等）。"""

    name: Identifier
    buses: tuple[VehicleBus, ...] = ()

    @model_validator(mode="after")
    def _validate_unique_buses(self) -> "VehicleVendor":
        names = [bus.bus_type for bus in self.buses]
        if len(names) != len(set(names)):
            raise ValueError(f"厂商 {self.name} 的总线类型不能重复")
        return self


class VehicleCapability(_Strict):
    """总类 vehicle：厂商 -> 总线 -> 硬件通道。"""

    vendors: tuple[VehicleVendor, ...] = ()

    @model_validator(mode="after")
    def _validate_unique_vendors(self) -> "VehicleCapability":
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
    minimum_channels: int | None = Field(default=None, ge=0)
    required_channels: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _validate_requirement(self) -> "BusRequirement":
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
    def _validate_requirement(self) -> "VehicleRequirement":
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
