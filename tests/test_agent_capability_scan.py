"""Agent 系统和语言运行时能力扫描测试。"""

from __future__ import annotations

from agent.application.services.capability_loader import (
    _is_version_like,
    _probe_version,
    _to_version,
    _total_memory_mb,
    scan_base_capabilities,
)


def test_scan_base_capabilities_populates_system_and_language() -> None:
    capabilities = scan_base_capabilities()

    assert capabilities.system is not None
    assert capabilities.system.operating_system is not None
    assert capabilities.system.operating_system.name
    assert capabilities.system.cpu_cores is None or capabilities.system.cpu_cores >= 0
    assert capabilities.system.memory_mb is None or capabilities.system.memory_mb > 0
    assert capabilities.language is not None
    assert any(runtime.name == "python" for runtime in capabilities.language.runtimes)


def test_total_memory_mb_is_positive_or_unavailable() -> None:
    memory = _total_memory_mb()

    assert memory is None or memory > 0


def test_probe_version_accepts_runtime_candidates() -> None:
    assert _probe_version(("python", "python3", "py")) is not None


def test_version_parser_accepts_only_dotted_numbers() -> None:
    assert _is_version_like("The command could not be loaded") is False
    assert _is_version_like("3.11.4") is True
    assert _is_version_like("v24.18.0") is True
    assert _is_version_like("17") is False
    assert _to_version("10.0.19045").root == "10.0.19045"
    assert _to_version("Darwin Kernel Version 24.0.0").root == "24.0.0"
    assert _to_version("not a version").root == "0"
