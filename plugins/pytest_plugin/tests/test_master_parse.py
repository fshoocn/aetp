"""Master 面 pytest 用例解析单测。"""

from __future__ import annotations

import pytest

from plugins.pytest_plugin.master.executor import (
    PytestMasterExecutor,
    create_executor,
)


class TestNodeIdParsing:
    def test_extracts_nodeids_and_skips_summary_lines(self, master_executor) -> None:
        stdout = """============================= test session starts =============================
platform win32 -- Python 3.11.9
collecting ... collected 3 items
<Module test_sample.py>
  <Function test_passes>
  <Function test_fails>
  <Function test_skips>

tests/test_sample.py::test_passes
tests/test_sample.py::test_fails
tests/test_sample.py::test_skips
============================== 3 passed in 0.03s ==============================
"""
        cases = PytestMasterExecutor._parse_nodeids(stdout, 0, "")
        keys = [case["stable_key"] for case in cases]
        assert keys == [
            "tests/test_sample.py::test_passes",
            "tests/test_sample.py::test_fails",
            "tests/test_sample.py::test_skips",
        ]
        assert all(case["name"] for case in cases)
        assert all("parent_path" in case for case in cases)
        # name 只取函数名，parent_path 只取文件段
        assert cases[0]["name"] == "test_passes"
        assert cases[0]["parent_path"] == "tests/test_sample.py"

    def test_deduplicates_repeated_nodeids(self, master_executor) -> None:
        stdout = "a.py::t1\na.py::t1\na.py::t2\n"
        cases = PytestMasterExecutor._parse_nodeids(stdout, 0, "")
        assert [c["stable_key"] for c in cases] == ["a.py::t1", "a.py::t2"]

    def test_parametrized_nodeid_keeps_full_id(self, master_executor) -> None:
        stdout = "test_a.py::test_add[1-2]\n"
        cases = PytestMasterExecutor._parse_nodeids(stdout, 0, "")
        assert cases[0]["stable_key"] == "test_a.py::test_add[1-2]"
        assert cases[0]["name"] == "test_add[1-2]"

    def test_no_tests_exit_code_without_nodeids_returns_empty(self, master_executor) -> None:
        # exit 5 = no tests collected；_parse_nodeids 只负责解析，返回空 tuple，
        # "未解析出任何用例"错误由 parse_cases 依据空结果抛出。
        assert PytestMasterExecutor._parse_nodeids("no tests ran\n", 5, "") == ()

    def test_no_cases_parse_cases_raises(self, master_executor, monkeypatch, tmp_path) -> None:
        import asyncio
        import subprocess

        def fake_run(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=[],
                returncode=5,
                stdout="no tests ran\n",
                stderr="",
            )

        monkeypatch.setattr("plugins.pytest_plugin.master.executor.subprocess.run", fake_run)
        with pytest.raises(ValueError, match="未解析出任何用例"):
            asyncio.run(master_executor.parse_cases(str(tmp_path), {}))

    def test_failed_collection_raises_with_stderr(self, master_executor) -> None:
        with pytest.raises(ValueError, match="收集用例失败.*SyntaxError"):
            PytestMasterExecutor._parse_nodeids("", 2, "tests/test_bad.py:3: SyntaxError\n")


class TestConfigurationDefaults:
    def test_python_executable_defaults_to_current_interpreter(self) -> None:
        import sys

        assert PytestMasterExecutor._python_executable({}) == sys.executable
        assert PytestMasterExecutor._python_executable({"python_executable": ""}) == sys.executable
        assert PytestMasterExecutor._python_executable({"python_executable": "  "}) == sys.executable

    def test_python_executable_uses_configuration(self) -> None:
        assert (
            PytestMasterExecutor._python_executable({"python_executable": "C:/py/python.exe"})
            == "C:/py/python.exe"
        )

    def test_collect_timeout_default_and_validation(self) -> None:
        assert PytestMasterExecutor._collect_timeout({}) == 60
        assert PytestMasterExecutor._collect_timeout({"collect_timeout_s": 30}) == 30
        assert PytestMasterExecutor._collect_timeout({"collect_timeout_s": 0}) == 60
        assert PytestMasterExecutor._collect_timeout({"collect_timeout_s": "bad"}) == 60


class TestFactory:
    def test_create_executor_returns_parseable_object(self) -> None:
        executor = create_executor()
        assert isinstance(executor, PytestMasterExecutor)
        assert executor.plugin_version == "2.0.0"
        assert callable(getattr(executor, "parse_cases", None))
