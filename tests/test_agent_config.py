"""P5.1：AgentSettings / 配置组合根测试。"""

from __future__ import annotations

import re
from collections.abc import Generator
from pathlib import Path

import pytest

import agent.config as agent_config
from agent.config import AgentSettings

_NODE_ID = "01M1GNPT7TT01G8SPDKQ51TZAX"
_NODE_ID_PATTERN = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"


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
    assert settings.ledger_url == "sqlite:///data/agent-runtime.db"
    assert settings.max_concurrent_runs == 1
    assert settings.heartbeat_interval_s == 5
    assert settings.log_level == "INFO"


def test_from_env_file_reads_agent_prefix(tmp_path) -> None:
    env_file = _write_env(
        tmp_path,
        f"AETP_AGENT_NODE_ID={_NODE_ID}\n"
        "AETP_AGENT_NAME=CAN 台架 01\n"
        "AETP_AGENT_MQTT_HOST=broker.local\n"
        "AETP_AGENT_MQTT_PORT=8883\n"
        "AETP_AGENT_LEDGER_URL=sqlite:///data/agent.db\n"
        "AETP_AGENT_HEARTBEAT_INTERVAL_S=7\n"
        "AETP_AGENT_MAX_CONCURRENT_RUNS=2\n",
    )
    settings = AgentSettings.from_env_file(env_file)
    assert settings.node_id == _NODE_ID
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
    assert re.fullmatch(_NODE_ID_PATTERN, settings.node_id)
    assert settings.name == "file-only"


def test_relative_paths_resolve_against_env_dir(tmp_path) -> None:
    env_file = _write_env(
        tmp_path,
        "AETP_AGENT_LOG_FILE=logs/agent.log\nAETP_AGENT_MQTT_CA_CERT_PATH=emqxsl-ca.crt\n",
    )
    settings = AgentSettings.from_env_file(env_file)
    assert settings.log_file == tmp_path / "logs" / "agent.log"
    assert settings.mqtt_ca_cert_path == tmp_path / "emqxsl-ca.crt"


def test_configure_get_settings_roundtrip(tmp_path) -> None:
    env_file = _write_env(tmp_path, f"AETP_AGENT_NODE_ID={_NODE_ID}\n")
    settings = agent_config.configure(env_file)
    assert agent_config.get_settings() is settings
    assert settings.node_id == _NODE_ID


def test_get_settings_raises_before_configure() -> None:
    with pytest.raises(RuntimeError):
        agent_config.get_settings()


def test_settings_validate_rejects_missing_identity() -> None:
    with pytest.raises(ValueError, match="NODE_ID"):
        AgentSettings().validate()


def test_settings_derive_client_id_and_validate(tmp_path) -> None:
    env_file = _write_env(tmp_path, f"AETP_AGENT_NODE_ID={_NODE_ID}\n")
    settings = AgentSettings.from_env_file(env_file).validate()
    assert settings.mqtt_client_id == f"aetp-agent-{_NODE_ID}"


def test_missing_node_id_is_generated_and_persisted(tmp_path) -> None:
    env_file = _write_env(tmp_path, "AETP_AGENT_NAME=CAN 台架 01\n")

    first = AgentSettings.from_env_file(env_file).validate()
    assert re.fullmatch(_NODE_ID_PATTERN, first.node_id)
    assert f"AETP_AGENT_NODE_ID={first.node_id}" in env_file.read_text(encoding="utf-8")

    second = AgentSettings.from_env_file(env_file).validate()
    assert second.node_id == first.node_id
    assert second.mqtt_client_id == first.mqtt_client_id


def test_placeholder_node_id_is_replaced(tmp_path) -> None:
    env_file = _write_env(tmp_path, "AETP_AGENT_NODE_ID=<node-id>\n")

    settings = AgentSettings.from_env_file(env_file).validate()

    assert re.fullmatch(_NODE_ID_PATTERN, settings.node_id)
    assert f"AETP_AGENT_NODE_ID={settings.node_id}" in env_file.read_text(encoding="utf-8")
