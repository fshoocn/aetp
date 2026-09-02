"""pytest V2 Master executor entrypoint。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


class PytestV2MasterExecutor:
    """在 Master 侧收集 pytest nodeid，供 ScriptDefinition 创建流程使用。"""

    plugin_version = "2.0.0"

    async def parse_cases(
        self,
        script_dir: str | Path,
        configuration: Mapping[str, object],
    ) -> tuple[dict[str, str], ...]:
        root = Path(script_dir).resolve()
        executable = str(configuration.get("python_executable") or sys.executable)
        raw_timeout = configuration.get("collect_timeout_s", 60)
        timeout_s = raw_timeout if isinstance(raw_timeout, int) and not isinstance(raw_timeout, bool) else 60
        result = await asyncio.to_thread(
            subprocess.run,
            [executable, "-m", "pytest", "--collect-only", "-q", str(root)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if result.returncode not in (0, 5):
            raise ValueError(f"pytest 收集用例失败: {result.stderr[-2000:]}")
        cases: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            key = line.strip()
            if key and "::" in key and not key.startswith("="):
                cases.append({"stable_key": key, "name": key.rsplit("::", 1)[-1], "parent_path": key})
        return tuple(cases)


def create_executor() -> PytestV2MasterExecutor:
    return PytestV2MasterExecutor()


__all__ = ["PytestV2MasterExecutor", "create_executor"]
