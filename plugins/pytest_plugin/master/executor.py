"""pytest V2 Master executor：解析脚本用例（collect-only）。

在 Master 侧用指定的 Python 解释器执行 ``pytest --collect-only``，
把 pytest nodeid 转成 V2 ScriptDefinition 需要的稳定用例字段。

插件不接触平台数据库、MQTT 或 Web 状态；只读取平台在临时目录解包好的脚本。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

# pytest --collect-only -q 的合法退出码：0 = 有收集到用例；5 = 无用例被收集。
_PYTEST_NO_TESTS_EXIT_CODE = 5


class PytestMasterExecutor:
    """Master 面 pytest 用例解析器。

    ``parse_cases(script_dir, configuration)`` 返回由 pytest nodeid 解析而来的用例元组，
    每个用例是含 ``stable_key``/``name``/``parent_path`` 的映射。
    ``stable_key`` 即 pytest nodeid（如 ``tests/test_smoke.py::test_boot``），
    作为后续 Shard 选择、执行和结果关联的稳定标识。
    """

    plugin_version = "2.0.0"

    async def parse_cases(
        self,
        script_dir: str | Path,
        configuration: Mapping[str, object],
    ) -> tuple[dict[str, str], ...]:
        root = Path(script_dir).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"脚本目录不存在: {root}")
        executable = self._python_executable(configuration)
        timeout_s = self._collect_timeout(configuration)
        command = [executable, "-m", "pytest", "--collect-only", "-q", str(root)]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ValueError(
                f"找不到 Python 解释器: {executable!r}；请在任务配置中设置 python_executable"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"pytest 收集用例超时（>{timeout_s}s）：{root}") from exc

        cases = self._parse_nodeids(result.stdout, result.returncode, result.stderr)
        if not cases:
            detail = (result.stderr or result.stdout or "").strip()
            raise ValueError(
                f"pytest 未解析出任何用例（exit={result.returncode}）"
                + (f"：{detail[-2000:]}" if detail else "")
            )
        return cases

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    @staticmethod
    def _python_executable(configuration: Mapping[str, object]) -> str:
        raw = configuration.get("python_executable")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return sys.executable

    @staticmethod
    def _collect_timeout(configuration: Mapping[str, object]) -> int:
        raw = configuration.get("collect_timeout_s", 60)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            return raw
        return 60

    @staticmethod
    def _parse_nodeids(stdout: str, returncode: int, stderr: str) -> tuple[dict[str, str], ...]:
        """从 ``pytest --collect-only -q`` 输出中提取 nodeid 并稳定去重。"""
        if returncode not in (0, _PYTEST_NO_TESTS_EXIT_CODE):
            detail = (stderr or stdout or "").strip()
            raise ValueError(
                f"pytest 收集用例失败（exit={returncode}）"
                + (f"：{detail[-2000:]}" if detail else "")
            )
        seen: dict[str, dict[str, str]] = {}
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line or "::" not in line or line.startswith("="):
                continue
            # collect-only -q 的 nodeid 行不会以数字或 < 开头；跳过计数/警告类行。
            if line[0].isdigit() or line.startswith("<"):
                continue
            key = line
            name = key.rsplit("::", 1)[-1]
            parent = key.rsplit("::", 1)[0]
            seen.setdefault(
                key,
                {
                    "stable_key": key,
                    "name": name,
                    "parent_path": parent,
                },
            )
        return tuple(seen.values())


def create_executor() -> PytestMasterExecutor:
    return PytestMasterExecutor()


__all__ = ["PytestMasterExecutor", "create_executor"]
