"""pytest 插件自测共享 fixture。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from plugins.pytest_plugin.agent.executor import PytestExecutor
from plugins.pytest_plugin.master.executor import PytestMasterExecutor


class _FakeContext:
    """最小 V2 ExecutionContext 替身：只暴露插件用到的只读字段。"""

    def __init__(self, case_keys: tuple[str, ...] = ()) -> None:
        self.run_id = "01J-test-run-00000000000001"
        self.case_keys = list(case_keys)
        self.params: dict[str, object] = {}
        self.script_ref: dict[str, object] = {}


@pytest.fixture
def master_executor() -> PytestMasterExecutor:
    return PytestMasterExecutor()


@pytest.fixture
def agent_executor() -> PytestExecutor:
    return PytestExecutor()


@pytest.fixture
def fake_context() -> _FakeContext:
    return _FakeContext(
        (
            "test_sample.py::test_passes",
            "test_sample.py::test_fails",
            "test_sample.py::test_skips",
        )
    )


@pytest.fixture
def junit_xml_path(tmp_path: Path) -> Path:
    """构造一份含 passed/failed/skipped 三种结果的 JUnit XML。"""
    content = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="3" failures="1" errors="0" skipped="1" time="0.12">
  <testcase classname="test_sample" name="test_passes" file="test_sample.py" time="0.01"/>
  <testcase classname="test_sample" name="test_fails" file="test_sample.py" time="0.09">
    <failure message="assert 1 == 2">assert 1 == 2&#10;&gt;  +  where 1 = one()</failure>
  </testcase>
  <testcase classname="test_sample" name="test_skips" file="test_sample.py" time="0.02">
    <skipped message="skip reason here"/>
  </testcase>
</testsuite>
"""
    path = tmp_path / "junit.xml"
    path.write_text(content, encoding="utf-8")
    # 解析验证 XML 合法
    ET.parse(path)
    return path


@pytest.fixture
def junit_all_pass_xml_path(tmp_path: Path) -> Path:
    """构造一份全部通过的 JUnit XML。"""
    content = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="2" failures="0" errors="0" skipped="0" time="0.03">
  <testcase classname="test_sample" name="test_a" file="test_sample.py" time="0.01"/>
  <testcase classname="test_sample" name="test_b" file="test_sample.py" time="0.02"/>
</testsuite>
"""
    path = tmp_path / "junit_all_pass.xml"
    path.write_text(content, encoding="utf-8")
    ET.parse(path)
    return path


__all__ = [
    "_FakeContext",
    "master_executor",
    "agent_executor",
    "fake_context",
    "junit_xml_path",
    "junit_all_pass_xml_path",
]
