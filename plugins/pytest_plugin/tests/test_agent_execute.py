"""Agent 面 pytest 执行/分析单测。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from plugins.pytest_plugin.agent.executor import PytestExecutor, create_executor


class TestPytestArgsValidation:
    def test_accepts_plain_args(self, agent_executor) -> None:
        assert PytestExecutor._pytest_args({"pytest_args": ["-q", "-k", "smoke"]}) == ["-q", "-k", "smoke"]

    def test_rejects_platform_managed_args(self, agent_executor) -> None:
        for bad in ("--junitxml=out.xml", "--junitxml", "--rootdir=/tmp"):
            with pytest.raises(ValueError, match="不得覆盖"):
                PytestExecutor._pytest_args({"pytest_args": [bad]})

    def test_rejects_non_list_or_non_string(self, agent_executor) -> None:
        with pytest.raises(ValueError, match="字符串数组"):
            PytestExecutor._pytest_args({"pytest_args": "not-a-list"})
        with pytest.raises(ValueError, match="字符串数组"):
            PytestExecutor._pytest_args({"pytest_args": [1, 2]})


class TestJUnitParsing:
    def test_parse_case_classifies_outcomes(self, junit_xml_path, fake_context) -> None:
        root = ET.parse(junit_xml_path).getroot()
        by_name = {
            case.attrib["name"]: PytestExecutor._parse_case(case, fake_context)
            for case in root.iter("testcase")
        }

        assert by_name["test_passes"]["status"] == "passed"
        assert by_name["test_passes"]["error_summary"] is None

        assert by_name["test_fails"]["status"] == "failed"
        assert "assert 1 == 2" in (by_name["test_fails"]["error_summary"] or "")

        assert by_name["test_skips"]["status"] == "skipped"

    def test_case_key_roundtrips_to_plan_keys(self, junit_xml_path, fake_context) -> None:
        # fake_context.case_keys 里含这些用例的完整 nodeid；JUnit classname=test_sample
        root = ET.parse(junit_xml_path).getroot()
        case = next(case for case in root.iter("testcase") if case.attrib["name"] == "test_passes")
        item = PytestExecutor._parse_case(case, fake_context)
        assert item["case_key"] == "test_sample.py::test_passes"

    def test_case_key_falls_back_when_unmatched(self, fake_context) -> None:
        classname = "other_module"
        name = "test_unknown"
        assert PytestExecutor._case_key(classname, name, fake_context) == "other_module::test_unknown"


class TestAnalyzeResults:
    @staticmethod
    def _analyze(result: dict, context) -> dict:
        import asyncio

        return asyncio.run(PytestExecutor().analyze_results(result, context))

    def test_analyze_passed_result(self, junit_all_pass_xml_path, fake_context) -> None:
        result = {
            "return_code": 0,
            "timed_out": False,
            "report_path": str(junit_all_pass_xml_path),
            "output_tail": ["line1"],
            "artifact_paths": [{"path": str(junit_all_pass_xml_path), "kind": "report"}],
        }
        analysis = self._analyze(result, fake_context)
        assert analysis["passed"] is True
        assert analysis["metrics"]["total"] == 2
        assert analysis["metrics"]["passed"] == 2
        assert analysis["metrics"]["failed"] == 0
        assert len(analysis["case_results"]) == 2

    def test_analyze_failed_when_cases_failed(self, junit_xml_path, fake_context) -> None:
        # 混合 JUnit：1 passed / 1 failed / 1 skipped；pytest 返回码 1
        result = {
            "return_code": 1,
            "timed_out": False,
            "report_path": str(junit_xml_path),
            "artifact_paths": [],
        }
        analysis = self._analyze(result, fake_context)
        assert analysis["passed"] is False
        assert analysis["metrics"]["total"] == 3
        assert analysis["metrics"]["failed"] == 1
        assert analysis["metrics"]["skipped"] == 1

    def test_analyze_failed_by_return_code_even_when_cases_pass(
        self, junit_all_pass_xml_path, fake_context
    ) -> None:
        # 用例全过但 pytest 返回非 0（如收集告警后崩溃），仍应视为失败
        result = {
            "return_code": 2,
            "timed_out": False,
            "report_path": str(junit_all_pass_xml_path),
            "artifact_paths": [],
        }
        analysis = self._analyze(result, fake_context)
        assert analysis["passed"] is False

    def test_analyze_timed_out_not_passed(self, junit_xml_path, fake_context) -> None:
        result = {
            "return_code": -1,
            "timed_out": True,
            "report_path": str(junit_xml_path),
            "artifact_paths": [],
        }
        analysis = self._analyze(result, fake_context)
        assert analysis["passed"] is False
        assert analysis["data"]["timed_out"] is True

    def test_analyze_without_report_keeps_passed_consistent(self, fake_context) -> None:
        result = {
            "return_code": 0,
            "timed_out": False,
            "report_path": "",
            "artifact_paths": [],
        }
        analysis = self._analyze(result, fake_context)
        assert analysis["passed"] is True
        assert analysis["case_results"] == []
        assert analysis["metrics"]["total"] == 0


class TestAttachments:
    def test_collect_attachments_glob(self, agent_executor, tmp_path) -> None:
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "overview.html").write_text("<html></html>", encoding="utf-8")
        (tmp_path / "reports" / "detail.html").write_text("<html></html>", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
        config = {"artifact_paths": ["reports/*.html", "notes.txt", "missing/*.log"]}
        items = PytestExecutor._collect_attachments(config, tmp_path)
        paths = sorted(item["path"] for item in items)
        assert len(paths) == 3
        assert all(item["kind"] == "data" for item in items)
        names = {Path(path).name for path in paths}
        assert names == {"overview.html", "detail.html", "notes.txt"}

    def test_collect_attachments_rejects_bad_type(self, agent_executor, tmp_path) -> None:
        assert PytestExecutor._collect_attachments({"artifact_paths": "nope"}, tmp_path) == []


class TestFactory:
    def test_create_executor_returns_executable_object(self) -> None:
        executor = create_executor()
        assert isinstance(executor, PytestExecutor)
        assert executor.plugin_version == "2.0.0"
        for method in ("execute", "analyze_results", "cleanup", "cancel"):
            assert callable(getattr(executor, method, None))
