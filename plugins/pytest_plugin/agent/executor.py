"""pytest V2 Agent executor：按 Plan 的 case_keys 执行并产出统一结果。

Agent 侧在平台准备好的脚本工作目录里，用配置指定的 Python 解释器执行 pytest：
- 只执行当前 Shard Plan 给出的 pytest nodeid（``context.case_keys``），不重复执行整套脚本；
- 实时把 pytest 输出经 ``context.capture_log`` 采集为结构化日志，并定期检查取消；
- 生成 JUnit XML（``--junitxml`` 与 ``--rootdir`` 由平台托管，不允许插件覆盖）；
- ``analyze_results`` 解析 JUnit，把 case 结果回填成 V2 ``CaseResult`` 语义并汇总 metrics。

插件不接触平台数据库、MQTT 或 Web 状态；只通过 V2 ExecutionContext 交互。
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# 平台托管的参数，插件与用户配置都不能覆盖。
_PLATFORM_MANAGED_ARGS = {"--junitxml", "--rootdir"}
# pytest "collect 到 0 个用例" 的退出码；此时不应当作执行崩溃。
_PYTEST_NO_TESTS_EXIT_CODE = 5


class PytestExecutor:
    """Agent 面 pytest 执行器。"""

    plugin_version = "2.0.0"

    async def execute(self, context) -> Mapping[str, Any]:
        script_dir = self._script_dir(context)
        config = dict(context.params or {})
        executable = self._python_executable(config)
        report = script_dir / f".aetp-pytest-{context.run_id}.xml"
        command = [
            executable,
            "-m",
            "pytest",
            "--capture=tee-sys",
            *self._pytest_args(config),
            *(["--maxfail=1"] if config.get("fail_fast") is True else []),
            *[str(key) for key in context.case_keys],
            "--junitxml",
            str(report),
        ]
        timeout_s = self._timeout_seconds(config)
        await context.progress(0, "pytest", "开始执行 pytest")
        await context.log("info", "执行 pytest", {"command": command})

        process = await asyncio.to_thread(
            subprocess.Popen,
            command,
            cwd=str(script_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        output_tail: list[str] = []
        try:
            while True:
                await context.raise_if_cancelled()
                line = await asyncio.to_thread(process.stdout.readline)
                if not line:
                    break
                value = line.rstrip("\r\n")
                output_tail.append(value)
                del output_tail[:-200]
                await context.capture_log("pytest", value)
            return_code = await asyncio.to_thread(self._wait_with_timeout, process, timeout_s)
        except asyncio.CancelledError:
            self._terminate(process)
            raise
        except _ProcessTimeout:
            self._terminate(process)
            await context.log(
                "error",
                "pytest 执行超时",
                {"timeout_s": timeout_s, "report": str(report)},
            )
            return {
                "return_code": -1,
                "timed_out": True,
                "report_path": str(report),
                "output_tail": output_tail,
                "artifact_paths": [],
            }
        finally:
            if process.poll() is None:
                self._terminate(process)

        if return_code not in (0, _PYTEST_NO_TESTS_EXIT_CODE):
            await context.log(
                "error",
                "pytest 非零退出",
                {"return_code": return_code, "tail": "\n".join(output_tail[-50:])},
            )
        await context.progress(100, "pytest", "pytest 执行完成")
        artifact_paths = [{"path": str(report), "kind": "report"}] if report.is_file() else []
        artifact_paths.extend(self._collect_attachments(config, script_dir))
        return {
            "return_code": return_code,
            "timed_out": False,
            "report_path": str(report),
            "output_tail": output_tail,
            "artifact_paths": artifact_paths,
        }

    async def cancel(self) -> None:
        # Kernel 取消时会先取消 execute 的 asyncio task（触发上面 CancelledError 分支做进程清理），
        # 再调用本钩子；这里无额外跨进程状态需要处理。
        return None

    async def cleanup(self, context) -> None:
        del context  # 平台会在本阶段结束后统一清理脚本工作目录，插件无需额外动作。

    async def analyze_results(
        self,
        execution_result: Mapping[str, Any],
        context,
    ) -> Mapping[str, Any]:
        report_path = Path(str(execution_result.get("report_path", "")))
        case_results: list[dict[str, Any]] = []
        if report_path.is_file():
            root = ET.parse(report_path).getroot()
            for case in root.iter("testcase"):
                item = self._parse_case(case, context)
                case_results.append(item)
        failed = sum(item["status"] in {"failed", "error"} for item in case_results)
        # return_code<0 表示平台超时终止（无 JUnit 收尾），按失败处理。
        return_code = execution_result.get("return_code")
        timed_out = execution_result.get("timed_out") is True or (isinstance(return_code, int) and return_code < 0)
        passed = not timed_out and return_code == 0 and failed == 0
        return {
            "passed": passed,
            "case_results": case_results,
            "artifact_paths": list(execution_result.get("artifact_paths") or []),
            "metrics": {
                "total": len(case_results),
                "passed": sum(item["status"] == "passed" for item in case_results),
                "failed": failed,
                "skipped": sum(item["status"] == "skipped" for item in case_results),
            },
            "data": {
                "return_code": return_code,
                "timed_out": timed_out,
                "output_tail": (execution_result.get("output_tail") or [])[-100:],
            },
        }

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    @staticmethod
    def _script_dir(context) -> Path:
        script_dir = Path(str(context.script_ref.get("path", ""))).resolve()
        if not script_dir.is_dir():
            raise FileNotFoundError(f"本地脚本目录不存在: {script_dir}")
        return script_dir

    @staticmethod
    def _python_executable(config: Mapping[str, Any]) -> str:
        raw = config.get("python_executable")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return sys.executable

    @staticmethod
    def _timeout_seconds(config: Mapping[str, Any]) -> int | None:
        raw = config.get("timeout_s")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            return raw
        return None

    @classmethod
    def _pytest_args(cls, config: Mapping[str, Any]) -> list[str]:
        raw = config.get("pytest_args", [])
        if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
            raise ValueError("pytest_args 必须是字符串数组")
        if any(item.split("=", 1)[0] in _PLATFORM_MANAGED_ARGS for item in raw):
            raise ValueError("pytest_args 不得覆盖平台托管的 --junitxml 或 --rootdir")
        return list(raw)

    @staticmethod
    def _wait_with_timeout(process: subprocess.Popen[Any], timeout_s: int | None) -> int:
        """子进程等待并支持超时；超时抛 _ProcessTimeout。"""
        try:
            return process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise _ProcessTimeout from exc

    @staticmethod
    def _terminate(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()

    @classmethod
    def _collect_attachments(cls, config: Mapping[str, Any], script_dir: Path) -> list[dict[str, str]]:
        raw = config.get("artifact_paths", [])
        if not isinstance(raw, list):
            return []
        found: dict[str, str] = {}
        for pattern in raw:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            for match in sorted(glob.glob(str(script_dir / pattern))):
                path = Path(match)
                if path.is_file():
                    found.setdefault(str(path), str(path))
        return [{"path": path, "kind": "data"} for path in found.values()]

    @staticmethod
    def _parse_case(case: ET.Element, context) -> dict[str, Any]:
        name = case.attrib.get("name", "")
        classname = case.attrib.get("classname", "")
        key = PytestExecutor._case_key(classname, name, context)
        status = "passed"
        if case.find("failure") is not None:
            status = "failed"
        elif case.find("error") is not None:
            status = "error"
        elif case.find("skipped") is not None:
            status = "skipped"
        duration_ms = max(0, int(float(case.attrib.get("time", "0")) * 1000))
        error_summary = None
        for outcome_name in ("failure", "error"):
            outcome = case.find(outcome_name)
            if outcome is not None:
                error_summary = "".join(outcome.itertext()).strip() or outcome.attrib.get("message")
                break
        return {
            "case_key": key,
            "status": status,
            "duration_ms": duration_ms,
            "error_summary": error_summary,
        }

    @staticmethod
    def _case_key(classname: str, name: str, context) -> str:
        candidates = tuple(getattr(context, "case_keys", ()))
        for case_key in candidates:
            if case_key.rsplit("::", 1)[-1] == name and (
                not classname or Path(case_key.split("::", 1)[0]).stem in classname
            ):
                return case_key
        return f"{classname}::{name}".strip(":")


class _ProcessTimeout(Exception):
    """内部标记：pytest 子进程超时。"""


def create_executor() -> PytestExecutor:
    return PytestExecutor()


__all__ = ["PytestExecutor", "create_executor"]
