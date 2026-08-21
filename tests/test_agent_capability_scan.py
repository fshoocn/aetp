"""P5：Agent 节点能力自动扫描测试（§18.5）。"""

from __future__ import annotations

import json
from collections.abc import Callable

from agent.application.services.capability_loader import (
    CapabilityCache,
    _device_fingerprint,
    _group_buses_by_type,
    _is_version_like,
    _port_exists,
    _probe_version,
    _scan_serial,
    _to_version,
    _total_memory_mb,
    scan_capabilities,
    scan_vehicle,
)


def test_scan_system_populated():
    """系统能力自动扫描：操作系统/内存/CPU 均被填充。"""
    caps = scan_capabilities(serial_map_file=None)
    assert caps.system is not None
    assert caps.system.operating_system is not None
    assert caps.system.operating_system.name
    assert caps.system.cpu_cores is None or caps.system.cpu_cores >= 0
    assert caps.system.memory_mb is None or caps.system.memory_mb > 0


def test_total_memory_mb_positive():
    """内存检测返回正数或 None（平台差异）。"""
    memory = _total_memory_mb()
    assert memory is None or memory > 0


def test_scan_language_detects_python():
    """语言探测至少发现 python（本仓库环境必然有）。"""
    caps = scan_capabilities(serial_map_file=None)
    assert caps.language is not None
    names = [r.name for r in caps.language.runtimes]
    assert "python" in names


def test_probe_version_returns_dotted_version():
    """版本探测只接受点分数字版本。"""
    assert _probe_version("python", ("python", "python3", "py")) is not None
    # 错误消息不能当版本
    assert _is_version_like("The command could not be loaded") is False
    assert _is_version_like("3.11.4") is True
    assert _is_version_like("v24.18.0") is True
    assert _is_version_like("17") is False


def test_scan_serial_marks_missing_ports_disabled(tmp_path):
    """串口映射：不存在的端口标记为禁用，存在的端口启用。"""
    mapping = tmp_path / "serial_ports.json"
    mapping.write_text(
        json.dumps({"relay_board": "COM999999_NOT_EXIST", "psu": "COM30"}),
        encoding="utf-8",
    )
    caps = _scan_serial(mapping)
    assert caps is not None
    by_function = {p.function: p for p in caps.ports}
    assert by_function["relay_board"].enabled is False
    # COM30 是否存在取决于运行机器；只断言字段结构正确
    assert by_function["psu"].port == "COM30"


def test_port_exists_returns_bool():
    """端口存在性检查返回 bool。"""
    result = _port_exists("COM999999_NOT_EXIST")
    assert isinstance(result, bool)


def test_scan_capabilities_empty_serial_without_mapping(tmp_path):
    """无串口映射文件时 serial 为 None，不报错。"""
    caps = scan_capabilities(serial_map_file=tmp_path / "missing.json")
    assert caps.serial is None
    assert caps.system is not None


class _FakeChannel:
    def __init__(
        self,
        name: str,
        *,
        can: bool = False,
        lin: bool = False,
        ethernet: bool = False,
    ) -> None:
        self.name = name
        self.hw_channel = 0
        self.channel_index = 0
        self.can = can
        self.lin = lin
        self.flexray = False
        self.ethernet = ethernet


class _FakeDevice:
    def __init__(self, name: str, channels: list[_FakeChannel]) -> None:
        self.name = name
        self.channels = channels


def test_group_buses_by_type():
    """车载通道按总线类型分组：CAN/LIN/ETH 分属不同 VehicleBus，型号写入 hardware_model。"""
    devices = [
        _FakeDevice(
            "VN1640",
            [
                _FakeChannel("VN1640 Channel 1", can=True),
                _FakeChannel("VN1640 Channel 2", can=True),
                _FakeChannel("VN1640 Channel 3", lin=True),
            ],
        ),
        _FakeDevice(
            "VN5610",
            [_FakeChannel("VN5610 Channel 1", can=True), _FakeChannel("VN5610 Channel 2", ethernet=True)],
        ),
    ]

    buses = _group_buses_by_type(devices)

    by_type = {bus.bus_type: bus for bus in buses}
    assert set(by_type) == {"can", "lin", "ethernet"}
    # 通道名用「设备名 + 通道名」，可直接看出通道归属设备
    assert [ch.name for ch in by_type["can"].channels] == [
        "VN1640 Channel 1",
        "VN1640 Channel 2",
        "VN5610 Channel 1",
    ]
    assert [ch.hardware_model for ch in by_type["can"].channels] == ["VN1640", "VN1640", "VN5610"]
    assert [ch.name for ch in by_type["lin"].channels] == ["VN1640 Channel 3"]
    assert [ch.name for ch in by_type["ethernet"].channels] == ["VN5610 Channel 2"]


def test_scan_vehicle_returns_none_or_capability():
    """车载扫描：无 XL 硬件时返回 None，有硬件时返回强类型 VehicleCapability。"""
    result = scan_vehicle()
    assert result is None or result.vendors[0].name == "vector"


def test_to_version_normalizes():
    """版本字符串规范化：提取点分数字，无法提取时降级为 '0'。"""
    assert _to_version("10.0.19045").root == "10.0.19045"
    assert _to_version("Darwin Kernel Version 24.0.0").root == "24.0.0"
    assert _to_version("not a version").root == "0"


def test_device_fingerprint_is_deterministic():
    """设备指纹：同一环境两次计算一致（可插拔外设未变）。"""
    first = _device_fingerprint(serial_map_file=None)
    second = _device_fingerprint(serial_map_file=None)
    assert first == second


def test_capability_cache_reuses_cached_result(monkeypatch):
    """缓存命中：外设未变时复用缓存，不重新扫描。"""
    calls = {"n": 0}

    original = scan_capabilities

    def _fake_scan(serial_map_file=None):
        calls["n"] += 1
        return original(serial_map_file=serial_map_file)

    monkeypatch.setattr(
        "agent.application.services.capability_loader.scan_capabilities",
        _fake_scan,
    )

    cache = CapabilityCache(serial_map_file=None)
    first = cache.scan()
    second = cache.scan()

    assert first is second  # 命中缓存，返回同一对象
    assert calls["n"] == 1  # 只全量扫描一次


def test_capability_cache_invalidates(monkeypatch):
    """主动失效：invalidate 后下次 scan 强制重扫。"""
    calls = {"n": 0}

    original = scan_capabilities

    def _fake_scan(serial_map_file=None):
        calls["n"] += 1
        return original(serial_map_file=serial_map_file)

    monkeypatch.setattr(
        "agent.application.services.capability_loader.scan_capabilities",
        _fake_scan,
    )

    cache = CapabilityCache(serial_map_file=None)
    cache.scan()
    cache.scan()
    assert calls["n"] == 1

    cache.invalidate()
    cache.scan()
    assert calls["n"] == 2


class _FakeUSBMonitor:
    """fake usb-monitor：捕获回调并记录启停。"""

    def __init__(self) -> None:
        self.on_connect: Callable[[str, dict], None] | None = None
        self.on_disconnect: Callable[[str, dict], None] | None = None
        self.started = False
        self.stopped = False

    def start_monitoring(self, on_connect=None, on_disconnect=None, check_every_seconds=0.5) -> None:
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.started = True

    def stop_monitoring(self, timeout=1.0) -> None:
        self.stopped = True


def test_usb_monitoring_invalidates_on_change(monkeypatch):
    """USB 插拔事件驱动失效：on_connect/on_disconnect 回调使缓存失效。"""
    fake = _FakeUSBMonitor()

    import sys
    import types

    fake_module = types.ModuleType("usbmonitor")
    fake_module.USBMonitor = lambda: fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "usbmonitor", fake_module)

    cache = CapabilityCache(serial_map_file=None)
    started = cache.start_usb_monitoring()
    assert started is True
    assert fake.started is True

    # 首次扫描后，模拟 USB 插拔回调 → 缓存失效 → 下次重扫
    cache.scan()
    assert fake.on_connect is not None
    fake.on_connect("device-1", {"ID_MODEL": "VN1640"})
    # invalidate 后，缓存对象已清空（下次 scan 会重扫）
    assert cache._cached is None


def test_usb_monitoring_degrades_without_library(monkeypatch):
    """usb-monitor 未安装时优雅降级：返回 False，不影响能力扫描。"""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "usbmonitor":
            raise ImportError("no usbmonitor")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    cache = CapabilityCache(serial_map_file=None)
    started = cache.start_usb_monitoring()
    assert started is False
    assert cache._usb_monitor is None
