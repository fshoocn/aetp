from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from aetp_protocol.plugin import CaseInfo, PluginPackage
from main import PytestAgentPlugin, PytestMasterPlugin, package

from common.event_loop import run_with_selector


class _PluginContext:
    task_id = "T-1"
    shard_id = "SH-1"
    run_id = "R-1"
    node_id = "node-1"

    def __init__(
        self,
        script_path: Path | None = None,
        case_keys: list[str] | None = None,
    ) -> None:
        self.params: Mapping[str, Any] = {}
        self.script_ref: Mapping[str, Any] = {"path": str(script_path)} if script_path is not None else {}
        self.case_keys = list(case_keys or [])
        self.lines: list[str] = []

    async def progress(self, percent: int, stage: str, message: str = "") -> None:
        return None

    async def log(
        self,
        level: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        return None

    async def capture_log(
        self,
        stream: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self.lines.append(message)

    def is_cancelled(self) -> bool:
        return False

    async def raise_if_cancelled(self) -> None:
        return None


def test_package_contract() -> None:
    assert isinstance(package, PluginPackage)
    assert package.metadata.task_type == "pytest"
    assert package.metadata.plugin_version == "1.3.0"
    assert package.master.task_type == package.agent.task_type
    assert package.master.plugin_version == package.agent.plugin_version
    assert package.metadata.ui == {
        "config_page": "pytest",
        "entry": "index.html",
        "task_config_entry": "task-config.html",
        "min_frontend_version": "0.1.0",
        "protocol_version": 1,
    }
    assert (Path(__file__).parents[1] / "ui" / "index.html").is_file()
    assert (Path(__file__).parents[1] / "ui" / "task-config.html").is_file()


def test_verify_script_requires_pytest_file(tmp_path: Path) -> None:
    plugin = PytestMasterPlugin()
    assert plugin.verify_script(str(tmp_path), {})
    (tmp_path / "test_sample.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    assert plugin.verify_script(str(tmp_path), {}) == []


def test_single_python_file_is_materialized_with_pytest_name(tmp_path: Path) -> None:
    from master.application.services.script_service import ScriptService

    ScriptService._unpack(b"def test_ok(): pass\n", "demo.py", tmp_path)

    assert (tmp_path / "test_script.py").is_file()
    assert not (tmp_path / "demo.py").exists()
    assert PytestMasterPlugin().verify_script(str(tmp_path), {}) == []


def test_split_shards_by_case_count() -> None:
    plugin = PytestMasterPlugin()
    cases = [CaseInfo(stable_key=f"test_{i}", name=f"test_{i}") for i in range(5)]
    shards = __import__("asyncio").run(plugin.split_shards(cases, {"cases_per_shard": 2}, {}))
    assert [shard.case_keys for shard in shards] == [
        ("test_0", "test_1"),
        ("test_2", "test_3"),
        ("test_4",),
    ]


def test_split_shards_forwards_execution_config() -> None:
    plugin = PytestMasterPlugin()
    cases = [CaseInfo(stable_key="test_0", name="test_0")]
    shards = __import__("asyncio").run(plugin.split_shards(cases, {"cases_per_shard": 1}, {"pytest_args": ["-q"]}))
    assert shards[0].execution_params == {"pytest_args": ["-q"]}


def test_agent_requires_cached_script(tmp_path: Path) -> None:
    plugin = PytestAgentPlugin()
    assert plugin.task_type == "pytest"
    assert plugin._pytest_args({"pytest_args": ["-q"]}) == ["-q"]
    with pytest.raises(ValueError):
        plugin._pytest_args({"pytest_args": "-q"})


def test_agent_shard_executes_only_selected_cases(tmp_path: Path) -> None:
    context = _PluginContext(tmp_path, ["test_probe.py::test_one", "test_probe.py::test_two"])
    assert PytestAgentPlugin()._case_args(context, tmp_path, {}) == [
        "test_probe.py::test_one",
        "test_probe.py::test_two",
    ]


def test_config_rejects_platform_owned_pytest_arguments() -> None:
    with pytest.raises(ValueError, match="junitxml"):
        PytestMasterPlugin._validate_config({"pytest_args": ["--junitxml=custom.xml"]})


def test_agent_execute_works_with_selector_loop(tmp_path: Path) -> None:
    script = tmp_path / "test_probe.py"
    script.write_text("def test_probe():\n    assert True\n", encoding="utf-8")

    context = _PluginContext(tmp_path)

    async def execute():
        return await PytestAgentPlugin().execute(context)

    result = run_with_selector(execute())
    assert result["return_code"] == 0
    assert Path(result["report_path"]).is_file()
    assert context.lines


def test_analyze_results_allows_skipped_and_keeps_case_output(tmp_path: Path) -> None:
    report = tmp_path / "report.xml"
    report.write_text(
        "<testsuite>"
        '<testcase classname="test_script" name="test_ok" time="0.01">'
        "<system-out>print from case</system-out>"
        "<system-err>log from case</system-err>"
        "</testcase>"
        '<testcase classname="test_script" name="test_skip" time="0">'
        '<skipped message="skip" />'
        "</testcase>"
        "</testsuite>",
        encoding="utf-8",
    )

    context = _PluginContext(case_keys=["test_script.py::test_ok", "test_script.py::test_skip"])

    result = __import__("asyncio").run(
        PytestAgentPlugin().analyze_results({"return_code": 0, "report_path": str(report)}, context)
    )
    assert result["passed"] is True
    assert result["case_results"][0]["case_key"] == "test_script.py::test_ok"
    assert result["case_results"][0]["detail"] == {
        "system-out": "print from case",
        "system-err": "log from case",
    }
    assert result["case_results"][1]["status"] == "skipped"
