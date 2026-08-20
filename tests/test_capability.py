"""P4.5 强类型能力模型与专用 matcher 测试。"""

from __future__ import annotations

import pytest
from aetp_protocol.capabilities import (
    BusRequirement,
    HardwareChannel,
    HardwareRequirements,
    LanguageCapability,
    LanguageRequirement,
    LanguageRuntime,
    NodeCapabilities,
    NumericConstraint,
    OperatingSystem,
    OperatingSystemRequirement,
    SerialCapability,
    SerialPortCapability,
    SerialPortRequirement,
    SystemCapability,
    SystemRequirement,
    VehicleBus,
    VehicleCapability,
    VehicleRequirement,
    VehicleVendor,
    Version,
    VersionConstraint,
)

from master.domain.capability import (
    AllOf,
    CapabilityEvaluator,
    LanguageSpec,
    SerialSpec,
    SystemSpec,
    VehicleSpec,
    evaluate_capability,
    list_capability_paths,
)


def _capabilities(
    *,
    can_channels: tuple[str, ...] = ("can0", "can1"),
    lin_channels: tuple[str, ...] = ("lin0",),
    python_version: str = "3.11",
    os_version: str = "10.0.19045",
    memory_mb: int = 16384,
    cpu_cores: int = 8,
    serial_enabled: bool = True,
) -> NodeCapabilities:
    return NodeCapabilities(
        vehicle=VehicleCapability(
            vendors=(
                VehicleVendor(
                    name="vector",
                    buses=(
                        VehicleBus(
                            bus_type="can",
                            channels=tuple(
                                HardwareChannel(name=name)
                                for name in can_channels
                            ),
                        ),
                        VehicleBus(
                            bus_type="lin",
                            channels=tuple(
                                HardwareChannel(name=name)
                                for name in lin_channels
                            ),
                        ),
                    ),
                ),
            )
        ),
        language=LanguageCapability(
            runtimes=(
                LanguageRuntime(name="python", version=Version(python_version)),
            )
        ),
        system=SystemCapability(
            operating_system=OperatingSystem(
                name="windows", version=Version(os_version)
            ),
            memory_mb=memory_mb,
            cpu_cores=cpu_cores,
        ),
        serial=SerialCapability(
            ports=(
                SerialPortCapability(
                    function="relay_board", port="20", enabled=serial_enabled
                ),
                SerialPortCapability(function="psu", port="30", enabled=True),
            )
        ),
    )


def _requirements() -> HardwareRequirements:
    return HardwareRequirements(
        vehicle=VehicleRequirement(
            all_of=(
                BusRequirement(
                    bus_type="can",
                    vendor="vector",
                    minimum_channels=2,
                    required_channels=("can0",),
                ),
                BusRequirement(bus_type="lin", minimum_channels=1),
            )
        ),
        languages=(
            LanguageRequirement(
                name="python",
                version=VersionConstraint(minimum=Version("3.11")),
            ),
        ),
        system=SystemRequirement(
            operating_system=OperatingSystemRequirement(
                name="windows",
                version=VersionConstraint(minimum=Version("10.0")),
            ),
            memory_mb=NumericConstraint(minimum=8192),
            cpu_cores=NumericConstraint(minimum=4),
        ),
        serial_ports=(
            SerialPortRequirement(function="relay_board", port="20"),
            SerialPortRequirement(function="psu"),
        ),
        required_tags=("can",),
    )


def test_strong_model_rejects_unknown_shape():
    with pytest.raises(ValueError):
        NodeCapabilities.model_validate({"unknown": {"value": 1}})
    with pytest.raises(ValueError):
        VehicleBus.model_validate({"bus_type": "can", "channels": [{"name": 1}]})


def test_version_model_rejects_non_numeric_version():
    with pytest.raises(ValueError):
        Version("windows-10")


def test_numeric_constraint_rejects_empty_or_invalid_bounds():
    with pytest.raises(ValueError):
        NumericConstraint()
    with pytest.raises(ValueError):
        NumericConstraint(minimum=10, maximum=2)


def test_bus_requirement_rejects_empty_requirement():
    with pytest.raises(ValueError):
        BusRequirement(bus_type="can")


def test_vehicle_model_rejects_duplicate_vendor_bus_and_channel_names():
    with pytest.raises(ValueError):
        VehicleCapability(
            vendors=(
                VehicleVendor(name="vector"),
                VehicleVendor(name="vector"),
            )
        )
    with pytest.raises(ValueError):
        VehicleVendor(
            name="vector",
            buses=(VehicleBus(bus_type="can"), VehicleBus(bus_type="can")),
        )
    with pytest.raises(ValueError):
        VehicleBus(
            bus_type="can",
            channels=(HardwareChannel(name="can0"), HardwareChannel(name="can0")),
        )


def test_vehicle_spec_checks_vendor_bus_count_and_channel_name():
    assert VehicleSpec().evaluate(_capabilities(), _requirements()) == ()
    assert VehicleSpec().evaluate(
        _capabilities(can_channels=("can0",)), _requirements()
    )


def test_vehicle_matcher_handles_lin_and_eth_as_data_not_logic():
    requirements = HardwareRequirements(
        vehicle=VehicleRequirement(
            all_of=(BusRequirement(bus_type="lin", required_channels=("lin0",)),)
        )
    )
    assert evaluate_capability(requirements, _capabilities()).matched is True
    eth_requirement = HardwareRequirements(
        vehicle=VehicleRequirement(
            all_of=(BusRequirement(bus_type="eth", minimum_channels=1),)
        )
    )
    assert evaluate_capability(eth_requirement, _capabilities()).matched is False


def test_vehicle_matcher_supports_explicit_any_of_vendor_alternatives():
    requirement = HardwareRequirements(
        vehicle=VehicleRequirement(
            any_of=(
                BusRequirement(bus_type="can", vendor="tongxing", minimum_channels=1),
                BusRequirement(bus_type="can", vendor="vector", minimum_channels=2),
            )
        )
    )
    assert evaluate_capability(requirement, _capabilities()).matched is True


def test_language_matcher_uses_semantic_versions():
    requirement = HardwareRequirements(
        languages=(
            LanguageRequirement(
                name="python",
                version=VersionConstraint(minimum=Version("3.10")),
            ),
        )
    )
    assert LanguageSpec().evaluate(
        _capabilities(python_version="3.11"), requirement
    ) == ()
    assert LanguageSpec().evaluate(
        _capabilities(python_version="3.9"), requirement
    )


def test_version_17_10_is_greater_than_17_2():
    requirement = HardwareRequirements(
        languages=(
            LanguageRequirement(
                name="python",
                version=VersionConstraint(minimum=Version("17.10")),
            ),
        )
    )
    assert evaluate_capability(
        requirement, _capabilities(python_version="17.2")
    ).matched is False
    assert evaluate_capability(
        requirement, _capabilities(python_version="17.10")
    ).matched is True


def test_system_matcher_checks_os_memory_and_cpu_separately():
    requirement = HardwareRequirements(
        system=SystemRequirement(
            operating_system=OperatingSystemRequirement(
                name="windows",
                version=VersionConstraint(minimum=Version("10.0.19045")),
            ),
            memory_mb=NumericConstraint(minimum=32768),
            cpu_cores=NumericConstraint(minimum=16),
        )
    )
    failures = SystemSpec().evaluate(_capabilities(), requirement)
    assert len(failures) == 2
    assert any("memory_mb" in failure for failure in failures)
    assert any("cpu_cores" in failure for failure in failures)


def test_serial_matcher_uses_function_as_identity_and_port_as_constraint():
    requirement = HardwareRequirements(
        serial_ports=(SerialPortRequirement(function="psu", port="30"),)
    )
    assert SerialSpec().evaluate(_capabilities(), requirement) == ()
    wrong_port = HardwareRequirements(
        serial_ports=(SerialPortRequirement(function="psu", port="20"),)
    )
    assert SerialSpec().evaluate(_capabilities(), wrong_port)


def test_serial_matcher_checks_enabled_state():
    requirement = HardwareRequirements(
        serial_ports=(SerialPortRequirement(function="relay_board"),)
    )
    assert SerialSpec().evaluate(
        _capabilities(serial_enabled=True), requirement
    ) == ()
    assert SerialSpec().evaluate(
        _capabilities(serial_enabled=False), requirement
    )


def test_aggregate_evaluator_combines_categories_and_tags():
    evaluator = CapabilityEvaluator()
    assert evaluator.evaluate(
        _capabilities(), _requirements(), tags=("can",)
    ).matched is True
    assert evaluator.evaluate(
        _capabilities(), _requirements(), tags=()
    ).matched is False


def test_evaluator_can_inject_custom_spec():
    evaluator = CapabilityEvaluator(
        spec=AllOf((VehicleSpec(), LanguageSpec(), SystemSpec(), SerialSpec()))
    )
    assert evaluator.evaluate(_capabilities(), HardwareRequirements()).matched is True


def test_list_capability_paths_is_model_aware():
    paths = list_capability_paths(_capabilities())
    assert "vehicle.vendor.vector.bus.can.channel.can0" in paths
    assert "language.python.version" in paths
    assert "serial.psu.port" in paths
