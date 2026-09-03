"""Python runtime 环境发现插件 Provider。

上报当前 Agent 解释器（``sys.executable``）的 Python 运行时能力。构造时支持注入
``discoverer``（测试用）或预先给定 ``runtimes``；默认探测当前解释器版本与可执行
文件路径。发现结果必须是 RuntimeCapability，且 provider_id/runtime_type 与
Provider 声明一致。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from aetp_protocol.capabilities import RuntimeCapability, Version
from aetp_protocol.discovery import RuntimeDiscoveryError

logger = logging.getLogger(__name__)


class PythonRuntimeProvider:
    """发现本机 Python 运行时（默认当前解释器）。"""

    provider_id = "org.aetp.python-runtime"
    runtime_type = "python"

    def __init__(
        self,
        *,
        runtimes: Iterable[RuntimeCapability] = (),
        discoverer: Callable[[], Iterable[RuntimeCapability]] | None = None,
    ) -> None:
        self._runtimes = tuple(runtimes)
        self._discoverer = discoverer

    def discover(self) -> tuple[RuntimeCapability, ...]:
        if self._discoverer is not None:
            discovered = self._validate(tuple(self._discoverer()))
            self._runtimes = discovered
            return discovered
        if not self._runtimes:
            current = self._current_interpreter()
            if current is not None:
                self._runtimes = (current,)
        return self._runtimes

    def _current_interpreter(self) -> RuntimeCapability | None:
        executable = _resolve_executable(sys.executable)
        if executable is None:
            return None
        version = _probe_python_version(executable)
        if version is None:
            return None
        return RuntimeCapability(
            provider_id=self.provider_id,
            runtime_id=f"python:{version.root}",
            runtime_type=self.runtime_type,
            version=version,
            executable_ref=str(Path(executable).resolve()),
        )

    def _validate(self, discovered: tuple[RuntimeCapability, ...]) -> tuple[RuntimeCapability, ...]:
        for item in discovered:
            if item.runtime_type != self.runtime_type:
                raise RuntimeDiscoveryError(
                    f"runtime_type 不一致: expected={self.runtime_type} actual={item.runtime_type}"
                )
            if item.provider_id != self.provider_id:
                raise RuntimeDiscoveryError(
                    f"provider_id 不一致: expected={self.provider_id} actual={item.provider_id}"
                )
        return discovered


def _resolve_executable(executable: str) -> str | None:
    if not executable:
        return None
    path = Path(executable)
    if path.exists():
        return str(path)
    resolved = shutil.which(executable)
    return resolved


def _probe_python_version(executable: str) -> Version | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        logger.warning("Python 版本探测失败: %s", executable)
        return None
    output = f"{result.stdout}\n{result.stderr}"
    for token in output.split():
        candidate = token.strip().rstrip(",").lstrip("v")
        parts = candidate.split(".")
        if len(parts) >= 2 and all(part.isdigit() for part in parts):
            return Version(candidate)
    return None


__all__ = ["PythonRuntimeProvider"]
