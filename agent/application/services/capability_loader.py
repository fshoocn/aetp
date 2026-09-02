"""Agent 基础能力兼容适配层（P5，V2 resource 扫描由插件负责）。

Agent 启动时**自动扫描**本机能力并随 ``node.register`` 上报，无需手工维护
能力 JSON：

- ``system``：自动检测操作系统 / 内存 / CPU（标准库实现，跨平台）；
- ``language``：自动检测已安装的运行时（python / java / node / dotnet 等，
  通过 ``shutil.which`` 探测可执行文件）；
- V2 的 CAN 和串口资源发现不在本模块实现，分别委托
    ``plugins.resource_providers.vector_can`` 与 ``plugins.resource_providers.serial``；
- ``scan_capabilities`` 仅作为 V1 兼容入口保留，V2 使用
    ``scan_base_capabilities`` 加 resource Provider Registry。

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
    SystemCapability,
    VehicleBus,
    VehicleCapability,
    Version,
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


def scan_base_capabilities() -> NodeCapabilities:
    """扫描 V2 核心负责的系统和语言运行时能力。"""
    return NodeCapabilities(
        system=_scan_system(),
        language=_scan_language(),
    )


# ---------------------------------------------------------------------------
# system：操作系统 / 内存 / CPU
# ---------------------------------------------------------------------------


def _scan_system() -> SystemCapability | None:
    """自动检测操作系统、内存与 CPU 核数（标准库实现）。"""
    try:
        os_name = platform.system().lower() or "unknown"
        os_version = _to_version(
            platform.version() or platform.release() or "0"
        )
        cpu_cores = os.cpu_count() or 0
        memory_mb = _total_memory_mb()
        return SystemCapability(
            operating_system=OperatingSystem(name=os_name, version=os_version),
            memory_mb=memory_mb,
            cpu_cores=cpu_cores,
        )
    except Exception:
        logger.exception("系统能力扫描失败")
        return None


def _to_version(raw: str) -> Version:
    """把任意版本字符串规范化为 ``Version``（点分数字，§5.1）。

    从字符串中提取第一个形如 ``x.y[.z]`` 的 token（如 ``10.0.19045``、
    ``Darwin Kernel Version 24.0.0`` -> ``24.0.0``）；无法提取时降级为
    ``"0"``，避免 ``Version`` 的 pattern 校验抛错。
    """
    for token in raw.split():
        candidate = token.strip().rstrip(",").lstrip("v")
        if _is_version_like(candidate):
            return Version(candidate)
    return Version("0")


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
        with open("/proc/meminfo", encoding="utf-8") as f:
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


def _probe_version(name: str, candidates: tuple[str, ...]) -> Version | None:
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
            candidate = token.strip().rstrip(",").lstrip("v")
            if _is_version_like(candidate):
                return Version(candidate)
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
    """兼容 V1 能力入口；具体串口扫描由 Agent resource 插件执行。"""
    from plugins.resource_providers.serial import scan_serial_ports

    return scan_serial_ports(_resolve_serial_map(serial_map_file))


def _resolve_serial_map(serial_map_file: str | Path | None) -> Path | None:
    """兼容 V1 能力入口；路径解析委托串口 resource 插件。"""
    from plugins.resource_providers.serial import resolve_serial_map

    if serial_map_file:
        return Path(serial_map_file)
    from agent.config import runtime_dir

    candidate = runtime_dir() / DEFAULT_SERIAL_MAP_FILE
    return resolve_serial_map(candidate)


def _port_exists(port: str) -> bool:
    """兼容 V1 能力入口；端口存在性由串口 resource 插件执行。"""
    from plugins.resource_providers.serial import port_exists_on_host

    return port_exists_on_host(port)


# ---------------------------------------------------------------------------
# vehicle：CAN 通道扫描（占位实现，由台架侧按需实现）
# ---------------------------------------------------------------------------


def scan_vehicle() -> VehicleCapability | None:
    """兼容 V1 能力入口；具体车载扫描由 Vector resource 插件执行。"""
    from plugins.resource_providers.vector_can import scan_vector_vehicle

    return scan_vector_vehicle()


def _group_buses_by_type(devices: list) -> list[VehicleBus]:
    """兼容 V1 测试入口；分组逻辑由 Vector resource 插件执行。"""
    from plugins.resource_providers.vector_can import group_buses_by_type

    return group_buses_by_type(devices)


def _channel_name(device, channel) -> str:
    """兼容 V1 测试入口；通道命名由 Vector resource 插件执行。"""
    from plugins.resource_providers.vector_can import channel_name

    return channel_name(device, channel)


# ---------------------------------------------------------------------------
# 能力缓存：仅在可插拔外设变动时重扫，避免每次心跳全量扫描
# ---------------------------------------------------------------------------


def _device_fingerprint(serial_map_file: str | Path | None) -> tuple:
    """计算可插拔外设的轻量指纹（串口端口 + Vector 硬件通道）。

    只探测「运行中可能热插拔」的外设：串口端口存在性与 Vector 设备通道。
    system / language 是安装状态，运行中不变，不参与指纹。指纹未变时
    复用缓存能力，避免每次全量扫描（尤其 language 的多次 subprocess）。
    """
    from plugins.resource_providers.serial import serial_fingerprint
    from plugins.resource_providers.vector_can import can_fingerprint

    return (serial_fingerprint(serial_map_file), can_fingerprint())


class CapabilityCache:
    """能力扫描缓存：指纹（可插拔外设）未变时复用缓存，变了才重扫。

    用法（Agent 容器装配为单例）::

        cache = CapabilityCache(serial_map_file)
        cache.start_usb_monitoring()  # 可选：USB 插拔事件驱动失效
        caps = cache.scan()   # 首次全量扫描
        caps = cache.scan()   # 指纹未变 -> 命中缓存

    能力更新由两条路径触发（互补）：
    1. **事件驱动**：``start_usb_monitoring`` 用 ``usb-monitor`` 跨平台库
       （Windows WMI / Linux pyudev / macOS IORegistry）后台监听 USB 插拔，
       Vector CAN 卡与 USB 串口都经 USB 连接，插拔时回调使缓存失效；
    2. **指纹兜底**：每次 ``scan()`` 对比轻量设备指纹，漏掉的事件也能兜住。
    system / language 是安装状态，运行中不变。
    """

    def __init__(self, serial_map_file: str | Path | None = None) -> None:
        self._serial_map_file = serial_map_file
        self._cached: NodeCapabilities | None = None
        self._fingerprint: tuple | None = None
        self._usb_monitor = None

    def scan(self) -> NodeCapabilities:
        """返回能力快照；指纹未变复用缓存，变了全量重扫。"""
        fingerprint = _device_fingerprint(self._serial_map_file)
        if self._cached is not None and fingerprint == self._fingerprint:
            logger.debug("能力缓存命中（外设未变动）")
            return self._cached

        logger.info("可插拔外设变动或首次扫描，重新扫描本机能力")
        capabilities = scan_capabilities(serial_map_file=self._serial_map_file)
        self._cached = capabilities
        self._fingerprint = fingerprint
        return capabilities

    def invalidate(self) -> None:
        """主动使缓存失效（下次 scan 强制重扫）。"""
        self._cached = None
        self._fingerprint = None

    # -- USB 插拔事件监听（可选，跨平台） -----------------------------------

    def start_usb_monitoring(self, check_every_seconds: float = 1.0) -> bool:
        """启动 USB 插拔监听（后台线程）；插拔时使缓存失效。

        ``usb-monitor`` 未安装或初始化失败时返回 ``False``（优雅降级，
        仅靠指纹兜底）；成功返回 ``True``。幂等：重复调用不重复启动。

        :param check_every_seconds: 后台轮询间隔（秒），默认 1.0。
        """
        if self._usb_monitor is not None:
            return True
        try:
            from usbmonitor import USBMonitor  # type: ignore[reportMissingImports]
        except Exception:  # noqa: BLE001 - usb-monitor 未安装则降级为纯指纹
            logger.warning("usb-monitor 未安装，USB 插拔事件监听不可用（仅指纹兜底）")
            return False

        try:
            monitor = USBMonitor()
            monitor.start_monitoring(
                on_connect=self._on_usb_change,
                on_disconnect=self._on_usb_change,
                check_every_seconds=check_every_seconds,
            )
        except Exception:  # noqa: BLE001 - 监听启动失败不阻塞 Agent
            logger.exception("USB 插拔监听启动失败，仅靠指纹兜底")
            return False

        self._usb_monitor = monitor
        logger.info("USB 插拔监听已启动（interval=%.1fs）", check_every_seconds)
        return True

    def stop_usb_monitoring(self) -> None:
        """停止 USB 插拔监听（Agent 关闭时调用）。"""
        if self._usb_monitor is None:
            return
        try:
            self._usb_monitor.stop_monitoring()
        except Exception:  # noqa: BLE001 - 停止失败不阻塞关闭
            logger.debug("停止 USB 监听异常（已忽略）", exc_info=True)
        self._usb_monitor = None
        logger.info("USB 插拔监听已停止")

    def _on_usb_change(self, device_id: str, device_info: dict) -> None:
        """USB 插拔回调：使缓存失效，下次 scan 重扫能力。"""
        model = (device_info or {}).get("ID_MODEL", "") if isinstance(device_info, dict) else ""
        logger.info("检测到 USB 设备变动，能力缓存失效: device=%s model=%s", device_id, model)
        self.invalidate()
