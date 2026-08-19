"""Agent 节点能力自动扫描（P5，§18.5）。

Agent 启动时**自动扫描**本机能力并随 ``node.register`` 上报，无需手工维护
能力 JSON：

- ``system``：自动检测操作系统 / 内存 / CPU（标准库实现，跨平台）；
- ``language``：自动检测已安装的运行时（python / java / node / dotnet 等，
  通过 ``shutil.which`` 探测可执行文件）；
- ``serial``：从**串口映射文件**读取「功能名 -> 端口号」映射，然后逐个检查
  端口当前是否存在（Windows 下 ``os.path.exists("COMx")`` 存在即端口可用）；
- ``vehicle``：CAN 通道扫描为**占位实现**，由台架侧按需实现
  （见 :func:`scan_vehicle`）。

串口映射文件示例（``serial_ports.json``）::

    {
      "relay_board": "COM20",
      "psu": "COM30",
      "oscilloscope": "COM40"
    }

能力扫描失败只告警不中断 Agent 启动——能力上报失败不应阻塞节点注册与心跳。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path

from aetp_protocol.capabilities import (
    LanguageCapability,
    LanguageRuntime,
    NodeCapabilities,
    OperatingSystem,
    SerialCapability,
    SerialPortCapability,
    SystemCapability,
)

logger = logging.getLogger(__name__)

# 需要探测的语言运行时：name -> 可执行文件候选名
_LANGUAGE_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", ("python", "python3", "py")),
    ("java", ("java",)),
    ("node", ("node", "nodejs")),
    ("dotnet", ("dotnet",)),
    ("go", ("go",)),
    ("gcc", ("gcc", "gcc.exe")),
    ("cmake", ("cmake",)),
    ("pytest", ("pytest",)),
)

# 串口映射文件名（相对 Agent 运行目录）
DEFAULT_SERIAL_MAP_FILE = "serial_ports.json"


def scan_capabilities(
    serial_map_file: str | Path | None = None,
) -> NodeCapabilities:
    """扫描本机能力并返回 ``NodeCapabilities``。

    :param serial_map_file: 串口映射文件路径；为 ``None`` 时尝试在 Agent
        运行目录查找 ``serial_ports.json``，找不到则跳过串口能力。
    """
    return NodeCapabilities(
        system=_scan_system(),
        language=_scan_language(),
        serial=_scan_serial(serial_map_file),
        vehicle=scan_vehicle(),
    )


# ---------------------------------------------------------------------------
# system：操作系统 / 内存 / CPU
# ---------------------------------------------------------------------------


def _scan_system() -> SystemCapability | None:
    """自动检测操作系统、内存与 CPU 核数（标准库实现）。"""
    try:
        os_name = platform.system().lower() or "unknown"
        os_version = platform.version() or platform.release() or "unknown"
        cpu_cores = os.cpu_count() or 0
        memory_mb = _total_memory_mb()
        return SystemCapability(
            operating_system=OperatingSystem(name=os_name, version=os_version),
            memory_mb=memory_mb,
            cpu_cores=cpu_cores,
        )
    except Exception:  # noqa: BLE001 - 扫描失败不阻塞启动
        logger.exception("系统能力扫描失败")
        return None


def _total_memory_mb() -> int | None:
    """返回总物理内存（MB）；无法检测时返回 None。"""
    try:
        if os.name == "nt":
            # Windows：GlobalMemoryStatusEx
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys // (1024 * 1024))
            return None
        # POSIX：/proc/meminfo
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb // 1024
        return None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# language：语言运行时探测
# ---------------------------------------------------------------------------


def _scan_language() -> LanguageCapability | None:
    """自动探测已安装的语言运行时（通过 ``shutil.which``）。"""
    runtimes: list[LanguageRuntime] = []
    for name, candidates in _LANGUAGE_PROBES:
        version = _probe_version(name, candidates)
        if version is not None:
            runtimes.append(LanguageRuntime(name=name, version=version))
    if not runtimes:
        return None
    return LanguageCapability(runtimes=tuple(runtimes))


def _probe_version(name: str, candidates: tuple[str, ...]) -> str | None:
    """探测可执行文件版本；找不到或执行失败返回 None。

    只接受匹配点分数字版本（如 ``3.11.4``、``17.0.10``）的输出；
    ``dotnet --version`` 在 SDK 缺失时输出的是错误消息，不能误当版本。
    """
    exe = shutil.which(candidates[0])
    if exe is None:
        return None
    try:
        result = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        output = (result.stdout or result.stderr or "").strip()
        # 提取版本号（如 "Python 3.11.4" -> "3.11.4"）；
        # 要求至少"数字.数字"，防止把错误消息当版本
        for token in output.split():
            if _is_version_like(token):
                return token
        return None
    except Exception:  # noqa: BLE001
        return None


def _is_version_like(token: str) -> bool:
    """判断 token 是否为点分数字版本（如 3.11.4 / 17.0.10 / 1.2）。"""
    parts = token.lstrip("v").split(".")
    if len(parts) < 2:
        return False
    return all(p.isdigit() for p in parts)


# ---------------------------------------------------------------------------
# serial：串口映射文件 + 存在性检查
# ---------------------------------------------------------------------------


def _scan_serial(serial_map_file: str | Path | None) -> SerialCapability | None:
    """从映射文件读取「功能名 -> 端口号」，并检查端口是否存在。

    映射文件为 JSON 对象：``{"relay_board": "COM20", "psu": "COM30"}``。
    仅保留当前存在的端口（``os.path.exists``，Windows 下 ``COMx`` 存在即可用）。
    """
    path = _resolve_serial_map(serial_map_file)
    if path is None or not path.exists():
        if path is not None:
            logger.warning("串口映射文件不存在，跳过串口能力: %s", path)
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.exception("串口映射文件解析失败: %s", path)
        return None

    if not isinstance(raw, dict):
        logger.warning("串口映射文件格式错误（应为 JSON 对象）: %s", path)
        return None

    ports: list[SerialPortCapability] = []
    for function, port in raw.items():
        if not isinstance(port, str) or not port:
            continue
        exists = _port_exists(port)
        ports.append(
            SerialPortCapability(function=function, port=port, enabled=exists)
        )
        if not exists:
            logger.warning("串口 %s (%s) 当前不存在，已标记为禁用", port, function)

    if not ports:
        return None
    return SerialCapability(ports=tuple(ports))


def _resolve_serial_map(serial_map_file: str | Path | None) -> Path | None:
    """解析串口映射文件路径；未指定时尝试 Agent 运行目录下的默认文件名。"""
    if serial_map_file:
        return Path(serial_map_file)
    # 未指定：尝试 Agent 运行目录下的默认文件
    from agent.config import runtime_dir

    candidate = runtime_dir() / DEFAULT_SERIAL_MAP_FILE
    return candidate if candidate.exists() else None


def _port_exists(port: str) -> bool:
    """检查串口端口当前是否存在。

    Windows 下 ``os.path.exists("COM20")`` 在端口存在时返回 True；
    POSIX 下检查 ``/dev/`` 设备节点。
    """
    try:
        if os.name == "nt":
            return os.path.exists(port) or os.path.exists(f"\\\\.\\{port}")
        return os.path.exists(port)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# vehicle：CAN 通道扫描（占位实现，由台架侧按需实现）
# ---------------------------------------------------------------------------


def scan_vehicle():
    """扫描 CAN 通道能力（占位实现）。

    .. note::
        当前返回 ``None``（不声明任何 CAN 能力）。台架侧需要实现时，在此
        返回 ``VehicleCapability``，例如::

            from aetp_protocol.capabilities import (
                HardwareChannel, VehicleBus, VehicleCapability, VehicleVendor,
            )
            return VehicleCapability(vendors=(
                VehicleVendor(name="vector", buses=(
                    VehicleBus(bus_type="can", channels=(
                        HardwareChannel(name="can0", enabled=True),
                        HardwareChannel(name="can1", enabled=True),
                    )),
                )),
            ))

        返回 ``None`` 表示不声明 CAN 能力。
    """
    return None
