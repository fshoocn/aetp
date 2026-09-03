"""Agent runtime/software 环境发现插件公共 SPI。

runtime/software 两类插件都只在 Agent 侧提供“本机事实发现”：把运行时实例
（Python/.NET 等）或外部商业软件（CANoe、Vector Driver 等）连同版本/许可状态
汇报进能力快照，供 Master 校验执行器插件的 ``static_requirements``，不参与资源
分配。因此 SPI 只有 ``provider_id`` 归属与 ``discover()`` 发现，不要求
activate/prepare 生命周期。

``provider_id`` 必须以来源插件 ``manifest.id`` 为命名空间：等于 ``manifest.id``
或以 ``manifest.id + "."`` 开头，用于把能力归属到其来源插件包。

能力模型本身在 :mod:`aetp_protocol.capabilities`（RuntimeCapability /
SoftwareCapability），这里只定义插件必须实现的 Protocol 与错误类型。
"""

from __future__ import annotations

from typing import Protocol

from .capabilities import RuntimeCapability, SoftwareCapability


class DiscoveryProviderError(RuntimeError):
    """runtime/software 发现 Provider 的业务边界错误。"""


class RuntimeDiscoveryError(DiscoveryProviderError):
    """runtime Provider 发现结果不符合契约。"""


class SoftwareDiscoveryError(DiscoveryProviderError):
    """software Provider 发现结果不符合契约。"""


class RuntimeProvider(Protocol):
    """Agent 侧 runtime 插件必须实现的发现接口。

    一个 Provider 负责发现一类运行时的全部本机实例；``runtime_type`` 声明其
    拥有的运行时类别（如 ``python``/``dotnet``）。``discover()`` 返回的每个
    ``RuntimeCapability`` 的 ``runtime_type`` 必须等于 ``self.runtime_type``、
    ``provider_id`` 必须等于 ``self.provider_id``。
    """

    provider_id: str
    runtime_type: str

    def discover(self) -> tuple[RuntimeCapability, ...]: ...


class SoftwareProvider(Protocol):
    """Agent 侧 software 插件必须实现的发现接口。

    一个 Provider 负责发现一个外部软件产品的安装/许可状态；``name`` 声明其
    拥有的软件名（如 ``CANoe``）。``discover()`` 返回的每个
    ``SoftwareCapability`` 的 ``name`` 必须等于 ``self.name``、``provider_id``
    必须等于 ``self.provider_id``。
    """

    provider_id: str
    name: str

    def discover(self) -> tuple[SoftwareCapability, ...]: ...


__all__ = [
    "DiscoveryProviderError",
    "RuntimeDiscoveryError",
    "SoftwareDiscoveryError",
    "RuntimeProvider",
    "SoftwareProvider",
]
