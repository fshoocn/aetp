"""Agent 进程级配置（组合根模式）。

Agent 是独立部署组件，配置**仅从外置 ``.env`` 文件读取**（``AETP_AGENT_``
前缀），不读取任意系统环境变量作为隐式覆盖；与 ``master/config.py`` 同构：

- 只在入口调用一次 ``configure()`` 显式初始化；
- 其余模块统一用 ``get_settings()`` 只读获取；
- 未初始化时 ``get_settings()`` 直接报错，绝不隐式猜测路径。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import sys

from common.config_utils import (
    load_env_file,
    parse_bool,
    parse_int,
    parse_task_types,
    resolve_sqlite_url as _resolve_sqlite_url,
)

_settings: "AgentSettings | None" = None
logger = logging.getLogger(__name__)


def runtime_dir() -> Path:
    """返回 Agent 组件运行目录（外部资源所在目录）。

    开发运行：agent/ 目录（config.py 所在目录，.env、data/、logs/ 均在此）；
    exe 冻结运行：exe 所在目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_sqlite_url(url: str) -> str:
    """将 SQLite 相对路径基于运行目录解析为绝对连接串。"""
    return _resolve_sqlite_url(url, runtime_dir())


@dataclass(frozen=True)
class AgentSettings:
    """Agent 组件配置（§9.7），全部来自外置 .env 的 AETP_AGENT_ 前缀。"""

    # ---- Agent 标识 ----
    node_id: str = ""
    name: str = ""
    # ---- MQTT 连接（与 Master 使用同一 Broker）----
    mqtt_host: str | None = None
    mqtt_port: int = 8883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_client_id: str = "aetp-agent"
    mqtt_ca_cert_path: Path | None = None
    mqtt_use_tls: bool = True
    # ---- 本地账本 ----
    ledger_url: str = "sqlite:///data/agent.db"
    # ---- 执行与心跳 ----
    max_concurrent_runs: int = 1
    heartbeat_interval_s: int = 5
    registration_timeout_s: int = 10
    supported_task_types: tuple[str, ...] = ()
    # ---- 运行日志 ----
    log_file: Path = Path("logs/agent.log")
    log_level: str = "INFO"
    log_console: bool = True
    # ---- 任务日志 spool ----
    task_log_batch_size: int = 50
    task_log_flush_s: int = 1
    task_log_spool_max_bytes: int = 104857600
    # ---- 实际使用的 env 文件路径（便于排查）----
    env_file: Path | None = None

    @classmethod
    def default_env_file(cls) -> Path:
        """返回外置 .env 路径；开发运行在 agent/ 下，exe 与 exe 同目录。"""
        return runtime_dir() / ".env"

    @classmethod
    def from_env_file(cls, env_file: str | Path | None = None) -> "AgentSettings":
        """纯工厂：仅从外置 env 文件读取并构造，不缓存、无副作用。"""
        path = (
            Path(env_file).resolve()
            if env_file is not None
            else cls.default_env_file()
        )
        values = load_env_file(path)
        base_dir = path.parent

        ca_cert = values.get("AETP_AGENT_MQTT_CA_CERT_PATH")
        ca_cert_path = None
        if ca_cert:
            p = Path(ca_cert)
            if not p.is_absolute():
                p = base_dir / p
            ca_cert_path = p

        raw_log_file = values.get("AETP_AGENT_LOG_FILE")
        log_file = Path(raw_log_file) if raw_log_file else cls.log_file
        if not log_file.is_absolute():
            log_file = base_dir / log_file

        return cls(
            node_id=values.get("AETP_AGENT_NODE_ID", cls.node_id),
            name=values.get("AETP_AGENT_NAME", cls.name),
            mqtt_host=values.get("AETP_AGENT_MQTT_HOST"),
            mqtt_port=parse_int(
                values.get("AETP_AGENT_MQTT_PORT"), cls.mqtt_port
            ),
            mqtt_username=values.get("AETP_AGENT_MQTT_USERNAME"),
            mqtt_password=values.get("AETP_AGENT_MQTT_PASSWORD"),
            mqtt_client_id=values.get(
                "AETP_AGENT_MQTT_CLIENT_ID", cls.mqtt_client_id
            ),
            mqtt_ca_cert_path=ca_cert_path,
            mqtt_use_tls=parse_bool(
                values.get("AETP_AGENT_MQTT_USE_TLS"), cls.mqtt_use_tls
            ),
            ledger_url=values.get("AETP_AGENT_LEDGER_URL", cls.ledger_url),
            max_concurrent_runs=parse_int(
                values.get("AETP_AGENT_MAX_CONCURRENT_RUNS"),
                cls.max_concurrent_runs,
            ),
            heartbeat_interval_s=parse_int(
                values.get("AETP_AGENT_HEARTBEAT_INTERVAL_S"),
                cls.heartbeat_interval_s,
            ),
            registration_timeout_s=parse_int(
                values.get("AETP_AGENT_REGISTRATION_TIMEOUT_S"),
                cls.registration_timeout_s,
            ),
            supported_task_types=parse_task_types(
                values.get("AETP_AGENT_SUPPORTED_TASK_TYPES")
            ),
            log_file=log_file,
            log_level=values.get("AETP_AGENT_LOG_LEVEL", cls.log_level),
            log_console=parse_bool(
                values.get("AETP_AGENT_LOG_CONSOLE"), cls.log_console
            ),
            task_log_batch_size=parse_int(
                values.get("AETP_AGENT_TASK_LOG_BATCH_SIZE"),
                cls.task_log_batch_size,
            ),
            task_log_flush_s=parse_int(
                values.get("AETP_AGENT_TASK_LOG_FLUSH_S"),
                cls.task_log_flush_s,
            ),
            task_log_spool_max_bytes=parse_int(
                values.get("AETP_AGENT_TASK_LOG_SPOOL_MAX_BYTES"),
                cls.task_log_spool_max_bytes,
            ),
            env_file=path,
        )

    from_dotenv = from_env_file


def configure(env_file: str | Path | None = None) -> AgentSettings:
    """组合根：进程启动时显式初始化全局配置（通常只在入口调用一次）。

    重复调用时：
    - 传入路径与已初始化路径一致：幂等返回当前配置；
    - 传入路径不同：抛出错误，避免出现两套配置并存。
    """
    global _settings
    if _settings is not None:
        requested = (
            Path(env_file).resolve() if env_file is not None else None
        )
        if requested is not None and requested != _settings.env_file:
            raise RuntimeError(
                f"配置已初始化（{_settings.env_file}），"
                f"不能再用 {requested} 重新初始化"
            )
        return _settings

    _settings = AgentSettings.from_env_file(env_file)
    logger.info("Agent 配置初始化完成: env_file=%s", _settings.env_file)
    return _settings


def get_settings() -> AgentSettings:
    """只读获取进程级配置；未初始化时明确报错而不是猜路径。"""
    if _settings is None:
        raise RuntimeError(
            "配置未初始化：请在入口先调用 agent.config.configure()"
        )
    return _settings


def reset_settings() -> None:
    """重置配置（测试用）。"""
    global _settings
    _settings = None
    logger.debug("Agent 配置已重置")
