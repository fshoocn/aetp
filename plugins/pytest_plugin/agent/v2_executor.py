"""pytest V2 executor entrypoint。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class PytestV2Executor:
    """在 Agent 工作目录执行精确 case_keys 并产出统一结果。"""

    plugin_version = "2.0.0"

    async def execute(self, context) -> Mapping[str, Any]:
        script_dir = Path(str(context.script_ref.get("path", ""))).resolve()
        if not script_dir.is_dir():
            raise FileNotFoundError(f"本地脚本目录不存在: {script_dir}")
        config = dict(context.params)
        executable = str(config.get("python_executable") or sys.executable)
        report = script_dir / f".aetp-pytest-{context.run_id}.xml"
        command = [
            executable,
            "-m",
            "pytest",
            "--capture=tee-sys",
            *self._pytest_args(config),
            *( ["--maxfail=1"] if config.get("fail_fast") is True else [] ),
            *[str(key) for key in context.case_keys],
            "--junitxml",
            str(report),
        ]
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
                del output_tail[:-100]
                await context.capture_log("pytest", value)
            return_code = await asyncio.to_thread(process.wait)
        except asyncio.CancelledError:
            process.terminate()
            await asyncio.to_thread(process.wait)
            raise
        await context.progress(100, "pytest", "pytest 执行完成")
        return {
            "return_code": return_code,
            "report_path": str(report),
            "output_tail": output_tail,
            "artifact_paths": [{"path": str(report), "kind": "report"}] if report.is_file() else [],
        }

    async def cancel(self) -> None:
        return None

    async def cleanup(self, context) -> None:
        del context

    async def analyze_results(self, execution_result: Mapping[str, Any], context) -> Mapping[str, Any]:
        report_path = Path(str(execution_result.get("report_path", "")))
        case_results: list[dict[str, Any]] = []
        if report_path.is_file():
            root = ET.parse(report_path).getroot()
            for case in root.iter("testcase"):
                key = self._case_key(case, context)
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
                case_results.append(
                    {
                        "case_key": key,
                        "status": status,
                        "duration_ms": duration_ms,
                        "error_summary": error_summary,
                    }
                )
        failed = sum(item["status"] in {"failed", "error"} for item in case_results)
        return {
            "passed": execution_result.get("return_code") == 0 and failed == 0,
            "case_results": case_results,
            "artifact_paths": execution_result.get("artifact_paths") or [],
            "metrics": {
                "total": len(case_results),
                "passed": sum(item["status"] == "passed" for item in case_results),
                "failed": failed,
                "skipped": sum(item["status"] == "skipped" for item in case_results),
            },
            "data": {"return_code": execution_result.get("return_code")},
        }

    @staticmethod
    def _pytest_args(config: Mapping[str, Any]) -> list[str]:
        raw = config.get("pytest_args", [])
        if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
            raise ValueError("pytest_args 必须是字符串数组")
        if any(item.split("=", 1)[0] in {"--junitxml", "--rootdir"} for item in raw):
            raise ValueError("pytest_args 不得覆盖平台管理的 --junitxml 或 --rootdir 参数")
        return list(raw)

    @staticmethod
    def _case_key(case: ET.Element, context) -> str:
        classname = case.attrib.get("classname", "")
        name = case.attrib.get("name", "")
        candidates = getattr(context, "case_keys", ())
        for case_key in candidates:
            if case_key.rsplit("::", 1)[-1] == name and (
                not classname or Path(case_key.split("::", 1)[0]).stem in classname
            ):
                return case_key
        return f"{classname}::{name}".strip(":")


def create_executor() -> PytestV2Executor:
    return PytestV2Executor()


__all__ = ["PytestV2Executor", "create_executor"]
