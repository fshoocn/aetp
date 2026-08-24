import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from common.config_utils import (
    load_env_file,
    parse_bool,
    parse_int,
)
from common.config_utils import (
    resolve_sqlite_url as _resolve_sqlite_url,
)

# 进程级配置（组合根模式）：
# - 只在入口（run.py）调用一次 configure() 显式初始化；
# - 其余模块统一用 get_settings() 只读获取；
# - 未初始化时 get_settings() 直接报错，绝不隐式猜路径。
_settings: "MasterSettings | None" = None
logger = logging.getLogger(__name__)

_DEV_JWT_SECRET = "aetp-master-dev-secret-change-me"
_MIN_JWT_SECRET_BYTES = 32


def runtime_dir() -> Path:
    """返回组件运行目录（外部资源所在目录）。

    开发运行：master/ 目录（config.py 所在目录，作为 Master 组件根，
    .env、data/、logs/ 均位于此）；
    exe 冻结运行：exe 所在目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# 项目根目录（仓库根）：master/config.py 的上一级。
# 集中定义一次，供 Alembic 迁移定位 alembic.ini / migrations 等仓库级资源使用，
# 避免在各模块散落 parents[N] 魔法索引。
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_sqlite_url(url: str) -> str:
    """将 SQLite 相对路径基于运行目录解析为绝对连接串。"""
    return _resolve_sqlite_url(url, runtime_dir())


# ---------------------------------------------------------------------------
# 分组配置子结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatabaseConfig:
    """数据库连接与迁移。"""
    url: str = "sqlite:///data/aetp.db"
    auto_migrate: bool = True


@dataclass(frozen=True)
class MqttConfig:
    """MQTT 消息代理连接。"""
    host: str | None = None
    port: int = 8883
    username: str | None = None
    password: str | None = None
    client_id: str = "aetp-master"
    ca_cert_path: Path | None = None
    use_tls: bool = True


@dataclass(frozen=True)
class HttpConfig:
    """HTTP 服务监听与对外地址。"""
    host: str = "127.0.0.1"
    port: int = 8000
    public_base_url: str = ""


@dataclass(frozen=True)
class LogConfig:
    """日志输出配置。"""
    level: str = "INFO"
    file: Path = Path("logs/aetp.log")
    console: bool = True


@dataclass(frozen=True)
class JwtConfig:
    """JWT 签发与验证。"""
    secret: str = "aetp-master-dev-secret-change-me"
    expire_minutes: int = 30
    issuer: str = "aetp-master"
    audience: str = "aetp-web"
    refresh_token_expire_days: int = 7


@dataclass(frozen=True)
class InternalSigningConfig:
    """内部签名下载端点（Agent 下载脚本包）。"""
    secret: str = ""
    download_ttl_s: int = 300


@dataclass(frozen=True)
class RunConfig:
    """Run 调度与维护参数。"""
    case_duration_default_s: float = 60.0
    case_duration_anomaly_percent: float = 300.0
    stale_timeout_s: int = 1800
    maintenance_interval_s: float = 30.0
    outbox_max_attempts: int = 5


@dataclass(frozen=True)
class BootstrapConfig:
    """首次启动自动创建管理员。"""
    admin_username: str = ""
    admin_password: str = ""
    admin_display_name: str = "Platform Admin"


# ---------------------------------------------------------------------------
# 主配置（组合各分组 + 顶层杂项）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MasterSettings:
    """Master 进程配置，字段按职责分组为嵌套 dataclass。

    为保持向后兼容，所有原有扁平属性名（如 ``settings.mqtt_host``）
    通过 property 委托到嵌套子对象。
    """

    # 外部数据目录（脚本/产物/插件存储根，默认运行目录下 data/）
    data_dir: Path | None = None
    # 实际使用的 env 文件路径（便于排查与传递）
    env_file: Path | None = None

    # 分组
    database: DatabaseConfig = DatabaseConfig()
    mqtt: MqttConfig = MqttConfig()
    http: HttpConfig = HttpConfig()
    log: LogConfig = LogConfig()
    jwt: JwtConfig = JwtConfig()
    internal_signing: InternalSigningConfig = InternalSigningConfig()
    run: RunConfig = RunConfig()
    bootstrap: BootstrapConfig = BootstrapConfig()

    # ---- 向后兼容 property（扁平访问） ----

    @property
    def database_url(self) -> str:
        return self.database.url

    @property
    def auto_migrate(self) -> bool:
        return self.database.auto_migrate

    @property
    def mqtt_host(self) -> str | None:
        return self.mqtt.host

    @property
    def mqtt_port(self) -> int:
        return self.mqtt.port

    @property
    def mqtt_username(self) -> str | None:
        return self.mqtt.username

    @property
    def mqtt_password(self) -> str | None:
        return self.mqtt.password

    @property
    def mqtt_client_id(self) -> str:
        return self.mqtt.client_id

    @property
    def mqtt_ca_cert_path(self) -> Path | None:
        return self.mqtt.ca_cert_path

    @property
    def mqtt_use_tls(self) -> bool:
        return self.mqtt.use_tls

    @property
    def http_host(self) -> str:
        return self.http.host

    @property
    def http_port(self) -> int:
        return self.http.port

    @property
    def public_base_url(self) -> str:
        return self.http.public_base_url

    @property
    def log_level(self) -> str:
        return self.log.level

    @property
    def log_file(self) -> Path:
        return self.log.file

    @property
    def log_console(self) -> bool:
        return self.log.console

    @property
    def jwt_secret(self) -> str:
        return self.jwt.secret

    @property
    def jwt_expire_minutes(self) -> int:
        return self.jwt.expire_minutes

    @property
    def jwt_issuer(self) -> str:
        return self.jwt.issuer

    @property
    def jwt_audience(self) -> str:
        return self.jwt.audience

    @property
    def refresh_token_expire_days(self) -> int:
        return self.jwt.refresh_token_expire_days

    @property
    def internal_signing_secret(self) -> str:
        return self.internal_signing.secret

    @property
    def internal_download_ttl_s(self) -> int:
        return self.internal_signing.download_ttl_s

    @property
    def case_duration_default_s(self) -> float:
        return self.run.case_duration_default_s

    @property
    def case_duration_anomaly_percent(self) -> float:
        return self.run.case_duration_anomaly_percent

    @property
    def run_stale_timeout_s(self) -> int:
        return self.run.stale_timeout_s

    @property
    def maintenance_interval_s(self) -> float:
        return self.run.maintenance_interval_s

    @property
    def outbox_max_attempts(self) -> int:
        return self.run.outbox_max_attempts

    @property
    def bootstrap_admin_username(self) -> str:
        return self.bootstrap.admin_username

    @property
    def bootstrap_admin_password(self) -> str:
        return self.bootstrap.admin_password

    @property
    def bootstrap_admin_display_name(self) -> str:
        return self.bootstrap.admin_display_name

    # ---- 工厂方法 ----

    @classmethod
    def default_env_file(cls) -> Path:
        """返回外置 .env 文件路径；开发运行在 master/ 下，exe 部署为 exe 同目录。"""
        return runtime_dir() / ".env"

    @classmethod
    def from_env_file(cls, env_file: str | Path | None = None) -> "MasterSettings":
        """纯工厂：仅从外置 env 文件读取并构造，不缓存、无副作用。

        通常不需要直接调用——入口应使用模块级 configure()，
        其余模块使用 get_settings()。
        """
        path = Path(env_file).resolve() if env_file is not None else cls.default_env_file()

        values = load_env_file(path)
        base_dir = path.parent

        # CA 证书路径：相对路径以 .env 所在目录为基准解析
        ca_cert = values.get("AETP_MASTER_MQTT_CA_CERT_PATH")
        ca_cert_path = None
        if ca_cert:
            p = Path(ca_cert)
            ca_cert_path = p if p.is_absolute() else base_dir / p

        # 日志路径同理
        raw_log_file = values.get("AETP_MASTER_LOG_FILE")
        log_file = cls.log.file  # type: ignore[attr-defined]
        if raw_log_file:
            p = Path(raw_log_file)
            log_file = p if p.is_absolute() else base_dir / p

        raw_port = values.get("AETP_MASTER_MQTT_PORT")
        raw_http_port = values.get("AETP_MASTER_HTTP_PORT")
        raw_jwt_expire = values.get("AETP_MASTER_JWT_EXPIRE_MINUTES")
        raw_download_ttl = values.get("AETP_MASTER_INTERNAL_DOWNLOAD_TTL_S")
        raw_case_duration_default = values.get("AETP_MASTER_CASE_DURATION_DEFAULT_S")
        raw_case_duration_anomaly = values.get("AETP_MASTER_CASE_DURATION_ANOMALY_PERCENT")
        raw_refresh_days = values.get("AETP_MASTER_REFRESH_TOKEN_EXPIRE_DAYS")
        raw_stale_timeout = values.get("AETP_MASTER_RUN_STALE_TIMEOUT_S")
        raw_maintenance_interval = values.get("AETP_MASTER_MAINTENANCE_INTERVAL_S")
        raw_outbox_max_attempts = values.get("AETP_MASTER_OUTBOX_MAX_ATTEMPTS")
        raw_data_dir = values.get("AETP_MASTER_DATA_DIR")
        data_dir = Path(raw_data_dir) if raw_data_dir else None

        result = cls(
            data_dir=data_dir,
            env_file=path,
            database=DatabaseConfig(
                url=values.get("AETP_MASTER_DATABASE_URL", DatabaseConfig.url),
                auto_migrate=parse_bool(values.get("AETP_MASTER_AUTO_MIGRATE"), DatabaseConfig.auto_migrate),
            ),
            mqtt=MqttConfig(
                host=values.get("AETP_MASTER_MQTT_HOST"),
                port=parse_int(raw_port, MqttConfig.port),
                username=values.get("AETP_MASTER_MQTT_USERNAME"),
                password=values.get("AETP_MASTER_MQTT_PASSWORD"),
                client_id=values.get("AETP_MASTER_MQTT_CLIENT_ID", MqttConfig.client_id),
                ca_cert_path=ca_cert_path,
                use_tls=parse_bool(values.get("AETP_MASTER_MQTT_USE_TLS"), MqttConfig.use_tls),
            ),
            http=HttpConfig(
                host=values.get("AETP_MASTER_HTTP_HOST", HttpConfig.host),
                port=parse_int(raw_http_port, HttpConfig.port),
                public_base_url=values.get("AETP_MASTER_PUBLIC_BASE_URL", HttpConfig.public_base_url),
            ),
            log=LogConfig(
                level=values.get("AETP_MASTER_LOG_LEVEL", LogConfig.level),
                file=log_file,
                console=parse_bool(values.get("AETP_MASTER_LOG_CONSOLE"), LogConfig.console),
            ),
            jwt=JwtConfig(
                secret=values.get("AETP_MASTER_JWT_SECRET", JwtConfig.secret),
                expire_minutes=parse_int(raw_jwt_expire, JwtConfig.expire_minutes),
                issuer=values.get("AETP_MASTER_JWT_ISSUER", JwtConfig.issuer),
                audience=values.get("AETP_MASTER_JWT_AUDIENCE", JwtConfig.audience),
                refresh_token_expire_days=parse_int(raw_refresh_days, JwtConfig.refresh_token_expire_days),
            ),
            internal_signing=InternalSigningConfig(
                secret=values.get("AETP_MASTER_INTERNAL_SIGNING_SECRET", InternalSigningConfig.secret),
                download_ttl_s=parse_int(raw_download_ttl, InternalSigningConfig.download_ttl_s),
            ),
            run=RunConfig(
                case_duration_default_s=(
                    float(raw_case_duration_default)
                    if raw_case_duration_default not in (None, "")
                    else RunConfig.case_duration_default_s
                ),
                case_duration_anomaly_percent=(
                    float(raw_case_duration_anomaly)
                    if raw_case_duration_anomaly not in (None, "")
                    else RunConfig.case_duration_anomaly_percent
                ),
                stale_timeout_s=parse_int(raw_stale_timeout, RunConfig.stale_timeout_s),
                maintenance_interval_s=(
                    float(raw_maintenance_interval)
                    if raw_maintenance_interval not in (None, "")
                    else RunConfig.maintenance_interval_s
                ),
                outbox_max_attempts=parse_int(raw_outbox_max_attempts, RunConfig.outbox_max_attempts),
            ),
            bootstrap=BootstrapConfig(
                admin_username=values.get("AETP_MASTER_BOOTSTRAP_ADMIN_USERNAME", BootstrapConfig.admin_username),
                admin_password=values.get("AETP_MASTER_BOOTSTRAP_ADMIN_PASSWORD", BootstrapConfig.admin_password),
                admin_display_name=values.get(
                    "AETP_MASTER_BOOTSTRAP_ADMIN_DISPLAY_NAME",
                    BootstrapConfig.admin_display_name,
                ),
            ),
        )

        return result

    from_dotenv = from_env_file


def configure(env_file: str | Path | None = None) -> MasterSettings:
    """组合根：进程启动时显式初始化全局配置（通常只在 run.py 调用一次）。

    重复调用时：
    - 传入路径与已初始化路径一致：幂等返回当前配置；
    - 传入路径不同：抛出错误，避免出现两套配置并存。
    """
    global _settings
    if _settings is not None:
        requested = Path(env_file).resolve() if env_file is not None else None
        if requested is not None and requested != _settings.env_file:
            raise RuntimeError(f"配置已初始化（{_settings.env_file}），不能再用 {requested} 重新初始化")
        return _settings

    _settings = MasterSettings.from_env_file(env_file)
    logger.info(
        "配置初始化完成: env_file=%s, database=%s, auto_migrate=%s",
        _settings.env_file,
        _settings.database_url,
        _settings.auto_migrate,
    )
    return _settings


def get_settings() -> MasterSettings:
    """只读获取进程级配置；未初始化时明确报错而不是猜路径。"""
    if _settings is None:
        raise RuntimeError("配置未初始化：请在入口（run.py）先调用 master.config.configure()")
    return _settings


def reset_settings() -> None:
    """重置配置（测试用）。"""
    global _settings
    _settings = None
    logger.debug("配置已重置")
