"""Agent 本机 Software 能力发现。"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterable

from aetp_protocol.capabilities import SoftwareCapability, Version

_SOFTWARE_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("CANoe", ("CANoe64.exe", "CANoe.exe", "canoe")),
    ("Vector Driver", ("vxlapi.dll",)),
)
_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+){1,3})(?!\d)")


def discover_software() -> tuple[SoftwareCapability, ...]:
    """探测常见台架软件；找不到或版本无法读取时不伪造能力。"""
    discovered: list[SoftwareCapability] = []
    for name, candidates in _SOFTWARE_PROBES:
        executable = _which(candidates)
        if executable is None:
            continue
        version = _probe_version(executable)
        if version is None:
            continue
        discovered.append(
            SoftwareCapability(
                provider_id="agent.discovery",
                name=name,
                version=version,
            )
        )
    return tuple(discovered)


def _which(candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable is not None:
            return executable
    return None


def _probe_version(executable: str) -> Version | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return None
    output = f"{result.stdout}\n{result.stderr}"
    match = _VERSION_PATTERN.search(output)
    return Version(match.group(1)) if match is not None else None
