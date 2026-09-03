"""Agent 系统和语言运行时能力扫描。"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import shutil
import subprocess

from aetp_protocol.capabilities import (
    LanguageCapability,
    LanguageRuntime,
    NodeCapabilities,
    OperatingSystem,
    SystemCapability,
    Version,
)

logger = logging.getLogger(__name__)

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


def scan_base_capabilities() -> NodeCapabilities:
    """扫描系统和语言运行时能力。"""
    return NodeCapabilities(
        system=_scan_system(),
        language=_scan_language(),
    )


def _scan_system() -> SystemCapability | None:
    try:
        os_name = platform.system().lower() or "unknown"
        os_version = _to_version(platform.version() or platform.release() or "0")
        return SystemCapability(
            operating_system=OperatingSystem(name=os_name, version=os_version),
            memory_mb=_total_memory_mb(),
            cpu_cores=os.cpu_count() or 0,
        )
    except Exception:
        logger.exception("系统能力扫描失败")
        return None


def _to_version(raw: str) -> Version:
    for token in raw.split():
        candidate = token.strip().rstrip(",").lstrip("v")
        if _is_version_like(candidate):
            return Version(candidate)
    return Version("0")


def _total_memory_mb() -> int | None:
    try:
        if os.name == "nt":
            from ctypes import wintypes

            class MemoryStatus(ctypes.Structure):
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

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys // (1024 * 1024))
            return None

        with open("/proc/meminfo", encoding="utf-8") as file:
            for line in file:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
        return None
    except Exception:
        return None


def _scan_language() -> LanguageCapability | None:
    runtimes: list[LanguageRuntime] = []
    for name, candidates in _LANGUAGE_PROBES:
        version = _probe_version(candidates)
        if version is not None:
            runtimes.append(LanguageRuntime(name=name, version=version))
    return LanguageCapability(runtimes=tuple(runtimes)) if runtimes else None


def _probe_version(candidates: tuple[str, ...]) -> Version | None:
    executable = next((shutil.which(candidate) for candidate in candidates if shutil.which(candidate)), None)
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        output = (result.stdout or result.stderr or "").strip()
        for token in output.split():
            candidate = token.strip().rstrip(",").lstrip("v")
            if _is_version_like(candidate):
                return Version(candidate)
    except Exception:
        return None
    return None


def _is_version_like(token: str) -> bool:
    parts = token.lstrip("v").split(".")
    return len(parts) >= 2 and all(part.isdigit() for part in parts)
