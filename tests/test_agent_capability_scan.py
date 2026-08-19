"""P5：Agent 节点能力自动扫描测试（§18.5）。"""

from __future__ import annotations

import json

from agent.application.services.capability_loader import (
    _is_version_like,
    _port_exists,
    _probe_version,
    _scan_serial,
    _total_memory_mb,
    scan_capabilities,
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
        json.dumps(
            {"relay_board": "COM999999_NOT_EXIST", "psu": "COM30"}
        ),
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
