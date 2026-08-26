"""AETP pytest ZIP 插件。

该文件是 ZIP 插件固定入口，必须导出名为 ``package`` 的 PluginPackage。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aetp_protocol.capabilities import HardwareRequirements
from aetp_protocol.plugin import (
    AgentTaskContext,
    CaseInfo,
    PluginMetadata,
    PluginPackage,
    ShardSpec,
    TaskDefinitionSpec,
)


class PytestMasterPlugin:
    task_type = "pytest"
    display_name = "pytest 自动化测试"
    plugin_version = "1.1.0"
    supported_versions = frozenset({"1.1.0"})
    config_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "pytest_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "附加 pytest 参数，不要重复填写测试路径或 JUnit 参数",
            },
            "python_executable": {"type": "string", "description": "留空使用 Agent 当前 Python"},
            "collect_timeout_s": {"type": "integer", "minimum": 1, "maximum": 3600, "default": 60},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 3600},
            "cases_per_shard": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 20},
            "test_path": {"type": "string", "description": "可选，脚本目录内的测试子目录或文件"},
            "fail_fast": {"type": "boolean", "default": False},
            "artifact_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "相对脚本目录的附件路径或 glob",
            },
        },
    }
    upload_spec: Mapping[str, Any] = {
        "extensions": [".py", ".zip"],
        "max_size_mb": 100,
        "required_files": ["test_*.py 或 *_test.py"],
    }

    @staticmethod
    def _validate_config(config: Mapping[str, Any]) -> None:
        raw_args = config.get("pytest_args", [])
        if not isinstance(raw_args, list) or not all(isinstance(item, str) and item.strip() for item in raw_args):
            raise ValueError("pytest_args 必须是非空字符串数组")
        if any(item.split("=", 1)[0] in {"--junitxml", "--rootdir"} for item in raw_args):
            raise ValueError("pytest_args 不得覆盖平台管理的 --junitxml 或 --rootdir 参数")
        executable = str(config.get("python_executable") or "").strip()
        if "\x00" in executable or len(executable) > 512:
            raise ValueError("python_executable 不合法")
        for key, maximum in (("collect_timeout_s", 3600), ("timeout_s", 86400), ("cases_per_shard", 1000)):
            if key in config and (isinstance(config[key], bool) or not isinstance(config[key], int) or not 1 <= config[key] <= maximum):
                raise ValueError(f"{key} 必须是 1-{maximum} 的整数")

    def verify_script(self, script_dir: str, config: Mapping[str, Any]) -> list[str]:
        root = Path(script_dir)
        if not root.exists():
            return [f"脚本目录不存在: {root}"]
        errors: list[str] = []
        test_path = str(config.get("test_path") or "").strip()
        if test_path:
            candidate = (root / test_path).resolve()
            if root.resolve() not in candidate.parents and candidate != root.resolve():
                errors.append("test_path 只能位于脚本目录内")
            elif not candidate.exists():
                errors.append(f"test_path 不存在: {test_path}")
        if not list(root.rglob("test_*.py")) and not list(root.rglob("*_test.py")):
            errors.append("未找到 pytest 测试文件（test_*.py 或 *_test.py）")
        try:
            self._validate_config(config)
        except ValueError as exc:
            errors.append(str(exc))
        if errors:
            return errors
        if not list(root.rglob("*.py")):
            return ["未找到 pytest 测试文件（test_*.py 或 *_test.py）"]
        return []

    async def parse_cases(self, script_dir: str, config: Mapping[str, Any]) -> list[CaseInfo]:
        root = Path(script_dir)
        executable = str(config.get("python_executable") or sys.executable)
        command = [executable, "-m", "pytest", "--collect-only", "-q", str(root)]
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            text=True,
            timeout=int(config.get("collect_timeout_s", 60)),
            cwd=str(root),
            check=False,
        )
        if result.returncode not in (0, 5):
            raise ValueError(f"pytest 收集用例失败: {result.stderr[-2000:]}")
        cases: list[CaseInfo] = []
        for line in result.stdout.splitlines():
            key = line.strip()
            if not key or key.startswith("=") or key.endswith(" tests collected"):
                continue
            if "::" in key:
                cases.append(CaseInfo(stable_key=key, name=key.rsplit("::", 1)[-1], parent_path=key))
        return cases

    def build_task_definition(self, config: Mapping[str, Any], cases: list[CaseInfo]) -> TaskDefinitionSpec:
        return TaskDefinitionSpec(
            default_case_keys=tuple(case.stable_key for case in cases),
            parameter_schema=dict(self.config_schema),
            split_policy={"type": "by_case_count", "cases_per_shard": int(config.get("cases_per_shard", 20))},
            timeout_s=int(config.get("timeout_s", 3600)),
            hardware_requirements=HardwareRequirements(),
        )

    async def split_shards(
        self, cases: list[CaseInfo], policy: Mapping[str, Any], config: Mapping[str, Any]
    ) -> list[ShardSpec]:
        size = int(policy.get("cases_per_shard", config.get("cases_per_shard", 20)))
        if size <= 0:
            raise ValueError("cases_per_shard 必须大于 0")
        return [
            ShardSpec(
                case_keys=tuple(case.stable_key for case in cases[index : index + size]),
                execution_params=dict(config),
            )
            for index in range(0, len(cases), size)
        ]

    def result_schema(self, config: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"type": "object", "required": ["status", "case_results"]}

    def hardware_requirements(self, config: Mapping[str, Any], cases: list[CaseInfo]) -> HardwareRequirements:
        return HardwareRequirements()


class PytestAgentPlugin:
    task_type = "pytest"
    display_name = "pytest 自动化测试"
    plugin_version = "1.1.0"
    supported_versions = frozenset({"1.1.0"})
    verify_location = "master"
    parse_location = "master"

    async def execute(self, context: AgentTaskContext) -> Mapping[str, Any]:
        script_dir = Path(str(context.script_ref.get("path", "")))
        if not script_dir.exists():
            raise FileNotFoundError(f"本地脚本缓存不存在: {script_dir}")
        config = dict(context.params)
        self._validate_config(config)
        executable = str(config.get("python_executable") or sys.executable)
        report = script_dir / f".aetp-pytest-{context.run_id}.xml"
        command = [
            executable,
            "-m",
            "pytest",
            "--capture=tee-sys",
            "-o",
            "log_cli=true",
            "-o",
            "junit_logging=all",
            *self._pytest_args(config),
            *( ["--maxfail=1"] if config.get("fail_fast") is True else [] ),
            *self._case_args(context, script_dir, config),
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
        lines: list[str] = []
        try:
            while True:
                await context.raise_if_cancelled()
                line = await asyncio.to_thread(process.stdout.readline)
                if not line:
                    break
                text = line.rstrip("\r\n")
                lines.append(text)
                await context.capture_log("pytest", text)
            return_code = await asyncio.to_thread(process.wait)
        except asyncio.CancelledError:
            process.terminate()
            await asyncio.to_thread(process.wait)
            raise
        await context.progress(100, "pytest", "pytest 执行完成")
        return {
            "return_code": return_code,
            "report_path": str(report),
            "output_tail": lines[-100:],
            "artifact_paths": self._artifact_paths(script_dir, config),
        }

    @staticmethod
    def _validate_config(config: Mapping[str, Any]) -> None:
        executable = str(config.get("python_executable") or "").strip()
        if executable and ("\x00" in executable or len(executable) > 512):
            raise ValueError("python_executable 不合法")
        for key, maximum in (("collect_timeout_s", 3600), ("timeout_s", 86400), ("cases_per_shard", 1000)):
            if key in config:
                value = config[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
                    raise ValueError(f"{key} 必须是 1-{maximum} 的整数")
        raw_args = config.get("pytest_args", [])
        if not isinstance(raw_args, list) or not all(isinstance(item, str) and item.strip() for item in raw_args):
            raise ValueError("pytest_args 必须是非空字符串数组")
        forbidden = {"--junitxml", "--rootdir"}
        if any(item.split("=", 1)[0] in forbidden for item in raw_args):
            raise ValueError("pytest_args 不得覆盖平台管理的 --junitxml 或 --rootdir 参数")

    @staticmethod
    def _case_args(context: AgentTaskContext, script_dir: Path, config: Mapping[str, Any]) -> list[str]:
        test_path = str(config.get("test_path") or "").strip()
        base = (script_dir / test_path).resolve() if test_path else script_dir.resolve()
        root = script_dir.resolve()
        if root not in base.parents and base != root:
            raise ValueError("test_path 只能位于脚本目录内")
        case_keys = [str(key).strip() for key in getattr(context, "case_keys", ()) if str(key).strip()]
        return case_keys or ([str(base)] if test_path else [])

    async def cancel(self) -> None:
        # ExecutionService 负责取消插件任务；这里保留接口供资源清理扩展。
        return None

    async def collect_logs(self, context: AgentTaskContext) -> None:
        return None

    async def analyze_results(self, execution_result: Any, context: AgentTaskContext) -> Mapping[str, Any]:
        report_path = Path(str(execution_result.get("report_path", "")))
        case_results: list[dict[str, Any]] = []
        # 报告路径无效（执行失败未生成报告）时优雅返回，不抛 PermissionError
        if report_path and report_path.is_file():
            try:
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
                    detail: dict[str, str] = {}
                    for output_name in ("system-out", "system-err"):
                        output = case.findtext(output_name, default="")
                        if output.strip():
                            detail[output_name] = output
                        skipped = case.find("skipped")
                        if skipped is not None and skipped.attrib.get("message"):
                            detail["skip_reason"] = skipped.attrib["message"]
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
                            "detail": detail or None,
                        }
                    )
            except Exception:  # noqa: BLE001 - 报告解析失败不阻断结果上报
                await context.log("error", "pytest 报告解析失败", {"report_path": str(report_path)})
        failed_count = sum(item["status"] in {"failed", "error"} for item in case_results)
        return {
            "passed": execution_result.get("return_code") == 0 and failed_count == 0,
            "case_results": case_results,
            "metrics": {
                "total": len(case_results),
                "passed": sum(item["status"] == "passed" for item in case_results),
                "failed": failed_count,
                "skipped": sum(item["status"] == "skipped" for item in case_results),
            },
            "data": {"return_code": execution_result.get("return_code")},
        }

    @staticmethod
    def _case_key(case: ET.Element, context: AgentTaskContext) -> str:
        classname = case.attrib.get("classname", "")
        name = case.attrib.get("name", "")
        parts = classname.split(".") if classname else []
        if parts:
            candidate = f"{parts[0]}.py"
            if len(parts) > 1:
                candidate += "::" + "::".join(parts[1:])
            candidate += f"::{name}"
            if candidate in getattr(context, "case_keys", ()):
                return candidate
        for case_key in getattr(context, "case_keys", ()):
            if case_key.rsplit("::", 1)[-1] == name:
                return case_key
        return f"{classname}::{name}".strip(":")

    @staticmethod
    def _artifact_paths(script_dir: Path, config: Mapping[str, Any]) -> list[dict[str, str]]:
        raw = config.get("artifact_paths", [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValueError("artifact_paths 必须是字符串数组")
        artifacts: list[dict[str, str]] = []
        root = script_dir.resolve()
        seen: set[Path] = set()
        for pattern in raw:
            candidate = Path(pattern)
            if candidate.is_absolute():
                raise ValueError("artifact_paths 只能使用脚本目录下的相对路径")
            for path in root.glob(pattern):
                resolved = path.resolve()
                if not resolved.is_file() or root not in resolved.parents or resolved in seen:
                    continue
                seen.add(resolved)
                artifacts.append({"path": str(resolved), "kind": "data"})
        return artifacts

    @staticmethod
    def _pytest_args(config: Mapping[str, Any]) -> list[str]:
        raw = config.get("pytest_args", [])
        if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
            raise ValueError("pytest_args 必须是字符串数组")
        return list(raw)


package = PluginPackage(
    metadata=PluginMetadata(
        task_type="pytest",
        plugin_version="1.1.0",
        supported_versions=frozenset({"1.1.0"}),
        display_name="pytest 自动化测试",
        config_schema=dict(PytestMasterPlugin.config_schema),
        upload_spec=dict(PytestMasterPlugin.upload_spec),
        ui={
            "config_page": "pytest",
            "entry": "index.html",
            "min_frontend_version": "0.1.0",
            "protocol_version": 1,
        },
    ),
    master=PytestMasterPlugin(),
    agent=PytestAgentPlugin(),
)
