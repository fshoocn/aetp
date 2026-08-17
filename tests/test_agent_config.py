"""P5.1：AgentSettings / 配置组合根测试。"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Generator

import pytest

import agent.config as agent_config
from agent.config import AgentSettings


@pytest.fixture(autouse=True)
def _reset_agent_settings() -> Generator[None, None, None]:
    """每个测试独立 Agent 配置全局状态。"""
    agent_config.reset_settings()
    yield
    agent_config.reset_settings()


def _write_env(tmp_path: Path, content: str) -> Path:
    env_file = tmp_path / "agent.env"
    env_file.write_text(content, encoding="utf-8")
    return env_file


def test_agent_settings_defaults() -> None:
    settings = AgentSettings()
    assert settings.node_id == ""
    assert settings.mqtt_port == 8883
    assert settings.mqtt_use_tls is True
    assert settings.ledger_url == "sqlite:///data/agent.db"
    assert settings.max_concurrent_runs == 1
    assert settings.heartbeat_interval_s == 5
    assert settings.supported_task_types == ()
    assert settings.log_level == "INFO"


def test_from_env_file_reads_agent_prefix(tmp_path) -> None:
    env_file = _write_env(
        tmp_path,
        "AETP_AGENT_NODE_ID=bench-001\n"
        "AETP_AGENT_NAME=CAN 台架 01\n"
        "AETP_AGENT_MQTT_HOST=broker.local\n"
        "AETP_AGENT_MQTT_PORT=8883\n"
        "AETP_AGENT_LEDGER_URL=sqlite:///data/agent.db\n"
        "AETP_AGENT_HEARTBEAT_INTERVAL_S=7\n"
        "AETP_AGENT_MAX_CONCURRENT_RUNS=2\n",
    )
    settings = AgentSettings.from_env_file(env_file)
    assert settings.node_id == "bench-001"
    assert settings.name == "CAN 台架 01"
    assert settings.mqtt_host == "broker.local"
    assert settings.mqtt_port == 8883
    assert settings.heartbeat_interval_s == 7
    assert settings.max_concurrent_runs == 2


def test_from_env_file_ignores_system_env(tmp_path, monkeypatch) -> None:
    """配置仅从 .env 文件读取，不读取系统环境变量。"""
    monkeypatch.setenv("AETP_AGENT_NODE_ID", "from-system-env")
    env_file = _write_env(tmp_path, "AETP_AGENT_NAME=file-only\n")
    settings = AgentSettings.from_env_file(env_file)
    assert settings.node_id != "from-system-env"
    assert settings.node_id.startswith("agent-")
    assert settings.name == "file-only"


def test_supported_task_types_parsing(tmp_path) -> None:
    env_file = _write_env(
        tmp_path, "AETP_AGENT_SUPPORTED_TASK_TYPES=can_test, flash_test\n"
    )
    settings = AgentSettings.from_env_file(env_file)
    assert settings.supported_task_types == ("can_test", "flash_test")


def test_relative_paths_resolve_against_env_dir(tmp_path) -> None:
    env_file = _write_env(
        tmp_path,
        "AETP_AGENT_LOG_FILE=logs/agent.log\n"
        "AETP_AGENT_MQTT_CA_CERT_PATH=emqxsl-ca.crt\n",
    )
    settings = AgentSettings.from_env_file(env_file)
    assert settings.log_file == tmp_path / "logs" / "agent.log"
    assert settings.mqtt_ca_cert_path == tmp_path / "emqxsl-ca.crt"


def test_configure_get_settings_roundtrip(tmp_path) -> None:
    env_file = _write_env(tmp_path, "AETP_AGENT_NODE_ID=bench-002\n")
    settings = agent_config.configure(env_file)
    assert agent_config.get_settings() is settings
    assert settings.node_id == "bench-002"


def test_get_settings_raises_before_configure() -> None:
    with pytest.raises(RuntimeError):
        agent_config.get_settings()


def test_settings_validate_rejects_missing_identity() -> None:
    with pytest.raises(ValueError, match="NODE_ID"):
        AgentSettings().validate()


def test_settings_derive_client_id_and_validate(tmp_path) -> None:
    env_file = _write_env(tmp_path, "AETP_AGENT_NODE_ID=bench-003\n")
    settings = AgentSettings.from_env_file(env_file).validate()
    assert settings.mqtt_client_id == "aetp-agent-bench-003"


def test_missing_node_id_is_generated_and_persisted(tmp_path) -> None:
    env_file = _write_env(tmp_path, "AETP_AGENT_NAME=CAN 台架 01\n")

    first = AgentSettings.from_env_file(env_file).validate()
    assert re.fullmatch(r"agent-[0-9a-f]{32}", first.node_id)
    assert f"AETP_AGENT_NODE_ID={first.node_id}" in env_file.read_text(
        encoding="utf-8"
    )

    second = AgentSettings.from_env_file(env_file).validate()
    assert second.node_id == first.node_id
    assert second.mqtt_client_id == first.mqtt_client_id


def test_placeholder_node_id_is_replaced(tmp_path) -> None:
    env_file = _write_env(tmp_path, "AETP_AGENT_NODE_ID=<node-id>\n")

    settings = AgentSettings.from_env_file(env_file).validate()

    assert settings.node_id != "<node-id>"
    assert f"AETP_AGENT_NODE_ID={settings.node_id}" in env_file.read_text(
        encoding="utf-8"
    )
