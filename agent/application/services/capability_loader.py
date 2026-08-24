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
    HardwareChannel,
    LanguageCapability,
    LanguageRuntime,
    NodeCapabilities,
    OperatingSystem,
    SerialCapability,
    SerialPortCapability,
    SystemCapability,
    VehicleBus,
    VehicleCapability,
    VehicleVendor,
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
    except Exception:
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
        ports.append(SerialPortCapability(function=function, port=port, enabled=exists))
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


def scan_vehicle() -> VehicleCapability | None:
    """扫描车载硬件能力（Vector CAN/LIN/FlexRay/Ethernet 通道）。

    通过 ``py-canoe`` 的 ``VxlDriver`` 读取 Vector XL 驱动发现的硬件设备与
    通道，映射为强类型能力树 ``VehicleCapability -> VehicleVendor -> VehicleBus
    -> HardwareChannel``（§18.5）。

    - 厂商名固定为 ``vector``（py-canoe 仅支持 Vector 硬件）；
    - 总线类型由通道的 ``can``/``lin``/``flexray``/``ethernet`` 能力位判定；
    - 通道名由「设备名 + 通道名」组成（如 ``Virtual Channel 1``），
      ``hardware_model`` 填设备型号（如 ``VN1640``）。

    扫描失败（py-canoe 未安装 / XL 驱动未就绪 / 无硬件）返回 ``None``，
    不阻塞 Agent 启动（与 system/language/serial 扫描一致）。
    """
    try:
        from py_canoe.helpers.vxlapi import VxlDriver
    except Exception:  # noqa: BLE001 - py-canoe 未安装则无车载能力
        logger.warning("py-canoe 未安装，跳过车载能力扫描")
        return None

    try:
        devices = VxlDriver().get_devices()
    except Exception:  # noqa: BLE001 - XL 驱动未就绪/无硬件
        logger.warning("Vector XL 驱动扫描失败，跳过车载能力")
        return None

    buses = _group_buses_by_type(devices)
    if not buses:
        return None
    return VehicleCapability(
        vendors=(
            VehicleVendor(name="vector", buses=tuple(buses)),
        )
    )


# 通道能力位 -> 总线类型（与 py-canoe XlBusType 对应，§18.5）
_BUS_TYPE_ATTRS: tuple[tuple[str, str], ...] = (
    ("can", "can"),
    ("lin", "lin"),
    ("flexray", "flexray"),
    ("ethernet", "ethernet"),
)


def _group_buses_by_type(devices: list) -> list[VehicleBus]:
    """把 Vector 硬件通道按总线类型分组，返回 ``VehicleBus`` 列表。

    一个设备可能提供多个总线类型的通道；按总线类型聚合。通道名由「设备名 +
    通道名」组成（如 ``Virtual Channel 1``），可直接看出通道归属设备；
    ``hardware_model`` 记录设备型号（如 ``VN1640``）。
    """
    by_type: dict[str, list[HardwareChannel]] = {}
    for device in devices:
        model = getattr(device, "name", None) or None
        for channel in getattr(device, "channels", []):
            for bus_type, attr in _BUS_TYPE_ATTRS:
                if getattr(channel, attr, False):
                    by_type.setdefault(bus_type, []).append(
                        HardwareChannel(
                            name=_channel_name(device, channel),
                            hardware_model=model,
                            enabled=True,
                        )
                    )
    buses: list[VehicleBus] = []
    for bus_type in ("can", "lin", "flexray", "ethernet"):
        channels = by_type.get(bus_type)
        if not channels:
            continue
        buses.append(VehicleBus(bus_type=bus_type, channels=tuple(channels)))
    return buses


def _channel_name(device, channel) -> str:
    """用「设备名 + 通道名」组成通道名（如 ``Virtual Channel 1``）。

    py-canoe 的 ``ChannelInfo.name`` 已包含设备名前缀（XL 驱动返回的完整
    通道名），直接使用即可保证唯一；通道名缺失时回退到硬件通道号拼接。
    """
    device_name = getattr(device, "name", "") or ""
    channel_name = getattr(channel, "name", "") or ""
    if channel_name:
        return channel_name
    if device_name:
        return f"{device_name} {getattr(channel, 'hw_channel', 0)}"
    return f"ch{getattr(channel, 'hw_channel', 0)}"


# ---------------------------------------------------------------------------
# 能力缓存：仅在可插拔外设变动时重扫，避免每次心跳全量扫描
# ---------------------------------------------------------------------------


def _device_fingerprint(serial_map_file: str | Path | None) -> tuple:
    """计算可插拔外设的轻量指纹（串口端口 + Vector 硬件通道）。

    只探测「运行中可能热插拔」的外设：串口端口存在性与 Vector 设备通道。
    system / language 是安装状态，运行中不变，不参与指纹。指纹未变时
    复用缓存能力，避免每次全量扫描（尤其 language 的多次 subprocess）。
    """
    serial_ports: list[tuple[str, str, bool]] = []
    path = _resolve_serial_map(serial_map_file)
    if path is not None and path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for function, port in raw.items():
                    if isinstance(port, str) and port:
                        serial_ports.append(
                            (function, port, _port_exists(port))
                        )
        except Exception:  # noqa: BLE001 - 指纹失败视为无串口
            serial_ports = []

    vehicle_channels: list[tuple[str, str, str]] = []
    try:
        from py_canoe.helpers.vxlapi import VxlDriver

        devices = VxlDriver().get_devices()
    except Exception:  # noqa: BLE001 - py-canoe 未安装/无硬件
        devices = []
    for device in devices:
        for channel in getattr(device, "channels", []):
            for bus_type, attr in _BUS_TYPE_ATTRS:
                if getattr(channel, attr, False):
                    vehicle_channels.append(
                        (
                            bus_type,
                            _channel_name(device, channel),
                            getattr(device, "name", "") or "",
                        )
                    )

    return (tuple(serial_ports), tuple(vehicle_channels))


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
