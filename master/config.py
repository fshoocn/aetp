
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
# - 未初始化时 get_settings() 直接报错，绝不隐式猜测路径。
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


@dataclass(frozen=True)
class MasterSettings:
    database_url: str = "sqlite:///data/aetp.db"
    # 外部数据目录（脚本/产物/插件存储根，默认运行目录下 data/）
    data_dir: Path | None = None
    mqtt_host: str | None = None
    mqtt_port: int = 8883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_client_id: str = "aetp-master"
    mqtt_ca_cert_path: Path | None = None
    mqtt_use_tls: bool = True
    # HTTP 服务监听地址与端口（从 .env 读取，默认 127.0.0.1:8000）
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    # 日志配置：默认写入运行目录 logs/aetp.log，同时输出控制台
    log_level: str = "INFO"
    log_file: Path = Path("logs/aetp.log")
    log_console: bool = True
    # JWT 签名密钥与过期时间（分钟）；访问令牌短、刷新令牌长（见 refresh_token_expire_days）
    jwt_secret: str = "aetp-master-dev-secret-change-me"
    jwt_expire_minutes: int = 30
    # JWT 签发方与受众（P2.9：解码时严格校验 iss/aud，防止令牌跨系统复用）
    jwt_issuer: str = "aetp-master"
    jwt_audience: str = "aetp-web"
    # 内部签名下载端点（P4.7）：Agent 经签名 URL 下载脚本包
    # public_base_url 为 Master 对外 HTTP 地址（run.assign 的 download_url 前缀）；空则生成相对路径
    public_base_url: str = ""
    # internal_signing_secret 为签名 HMAC 密钥；空则回退到 jwt_secret
    internal_signing_secret: str = ""
    # internal_download_ttl_s 为签名 URL 有效期（秒）
    internal_download_ttl_s: int = 300
    # P6.8：缺少 case 历史耗时时 by-time 分割使用的默认秒数
    case_duration_default_s: float = 60.0
    # P6.8：成功 case 新样本相对均值允许的最大偏离百分比
    case_duration_anomaly_percent: float = 300.0
    # 刷新令牌有效期（天）；刷新时轮换，登出/改密/禁用账户时撤销
    refresh_token_expire_days: int = 7
    # 数据库自动迁移（Alembic upgrade head）；生产可关闭改为部署脚本手动执行
    auto_migrate: bool = True
    # Run 超时检测阈值（秒）：非终态 Run 超过该时长标记 timed_out（§8.6 巡检）
    run_stale_timeout_s: int = 1800
    # 后台维护 worker 轮询间隔（秒）：Schedule tick + Stale Run 检测
    maintenance_interval_s: float = 30.0
    # Outbox 最大发送尝试次数（超过标记 exhausted）
    outbox_max_attempts: int = 5
    # 开启首次启动时自动创建 platform_admin（若 users 表为空则创建，由 .env 凭据指定）
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    bootstrap_admin_display_name: str = "Platform Admin"
    # 实际使用的 env 文件路径（便于排查与传递）
    env_file: Path | None = None

    @classmethod
    def default_env_file(cls) -> Path:
        """返回外置 .env 文件路径；开发运行在 master/ 下，exe 部署与 exe 同目录。"""
        return runtime_dir() / ".env"

    @classmethod
    def from_env_file(cls, env_file: str | Path | None = None) -> "MasterSettings":
        """纯工厂：仅从外置 env 文件读取并构造，不缓存、无副作用。

        通常不需要直接调用——入口应使用模块级 configure()，
        其余模块使用 get_settings()。
        """
        path = (
            Path(env_file).resolve()
            if env_file is not None
            else cls.default_env_file()
        )

        values = load_env_file(path)
        base_dir = path.parent

        # CA 证书路径：相对路径以 .env 所在目录为基准解析
        ca_cert = values.get("AETP_MASTER_MQTT_CA_CERT_PATH")
        ca_cert_path = None
        if ca_cert:
            p = Path(ca_cert)
            if not p.is_absolute():
                p = base_dir / p
            ca_cert_path = p

        raw_port = values.get("AETP_MASTER_MQTT_PORT")
        raw_http_port = values.get("AETP_MASTER_HTTP_PORT")
        raw_jwt_expire = values.get("AETP_MASTER_JWT_EXPIRE_MINUTES")
        raw_refresh_days = values.get("AETP_MASTER_REFRESH_TOKEN_EXPIRE_DAYS")
        raw_download_ttl = values.get("AETP_MASTER_INTERNAL_DOWNLOAD_TTL_S")
        raw_case_duration_default = values.get(
            "AETP_MASTER_CASE_DURATION_DEFAULT_S"
        )
        raw_case_duration_anomaly = values.get(
            "AETP_MASTER_CASE_DURATION_ANOMALY_PERCENT"
        )
        raw_log_file = values.get("AETP_MASTER_LOG_FILE")
        log_file = Path(raw_log_file) if raw_log_file else cls.log_file
        if not log_file.is_absolute():
            log_file = base_dir / log_file

        raw_stale_timeout = values.get("AETP_MASTER_RUN_STALE_TIMEOUT_S")
        raw_maintenance_interval = values.get("AETP_MASTER_MAINTENANCE_INTERVAL_S")
        raw_outbox_max_attempts = values.get("AETP_MASTER_OUTBOX_MAX_ATTEMPTS")

        raw_data_dir = values.get("AETP_MASTER_DATA_DIR")
        data_dir = Path(raw_data_dir) if raw_data_dir else None

        result = cls(
            database_url=values.get("AETP_MASTER_DATABASE_URL", cls.database_url),
            data_dir=data_dir,
            mqtt_host=values.get("AETP_MASTER_MQTT_HOST"),
            mqtt_port=parse_int(raw_port, cls.mqtt_port),
            mqtt_username=values.get("AETP_MASTER_MQTT_USERNAME"),
            mqtt_password=values.get("AETP_MASTER_MQTT_PASSWORD"),
            mqtt_client_id=values.get(
                "AETP_MASTER_MQTT_CLIENT_ID", cls.mqtt_client_id
            ),
            mqtt_ca_cert_path=ca_cert_path,
            mqtt_use_tls=parse_bool(
                values.get("AETP_MASTER_MQTT_USE_TLS"), cls.mqtt_use_tls
            ),
            http_host=values.get("AETP_MASTER_HTTP_HOST", cls.http_host),
            http_port=parse_int(raw_http_port, cls.http_port),
            log_level=values.get("AETP_MASTER_LOG_LEVEL", cls.log_level),
            log_file=log_file,
            log_console=parse_bool(
                values.get("AETP_MASTER_LOG_CONSOLE"), cls.log_console
            ),
            jwt_secret=values.get("AETP_MASTER_JWT_SECRET", cls.jwt_secret),
            jwt_expire_minutes=parse_int(
                raw_jwt_expire, cls.jwt_expire_minutes
            ),
            jwt_issuer=values.get("AETP_MASTER_JWT_ISSUER", cls.jwt_issuer),
            jwt_audience=values.get("AETP_MASTER_JWT_AUDIENCE", cls.jwt_audience),
            public_base_url=values.get(
                "AETP_MASTER_PUBLIC_BASE_URL", cls.public_base_url
            ),
            internal_signing_secret=values.get(
                "AETP_MASTER_INTERNAL_SIGNING_SECRET",
                cls.internal_signing_secret,
            ),
            internal_download_ttl_s=parse_int(
                raw_download_ttl, cls.internal_download_ttl_s
            ),
            case_duration_default_s=(
                float(raw_case_duration_default)
                if raw_case_duration_default not in (None, "")
                else cls.case_duration_default_s
            ),
            case_duration_anomaly_percent=(
                float(raw_case_duration_anomaly)
                if raw_case_duration_anomaly not in (None, "")
                else cls.case_duration_anomaly_percent
            ),
            refresh_token_expire_days=parse_int(
                raw_refresh_days, cls.refresh_token_expire_days
            ),
            auto_migrate=parse_bool(
                values.get("AETP_MASTER_AUTO_MIGRATE"), cls.auto_migrate
            ),
            run_stale_timeout_s=parse_int(
                raw_stale_timeout, cls.run_stale_timeout_s
            ),
            maintenance_interval_s=(
                float(raw_maintenance_interval)
                if raw_maintenance_interval not in (None, "")
                else cls.maintenance_interval_s
            ),
            outbox_max_attempts=parse_int(
                raw_outbox_max_attempts, cls.outbox_max_attempts
            ),
            bootstrap_admin_username=values.get(
                "AETP_MASTER_BOOTSTRAP_ADMIN_USERNAME", cls.bootstrap_admin_username
            ),
            bootstrap_admin_password=values.get(
                "AETP_MASTER_BOOTSTRAP_ADMIN_PASSWORD", cls.bootstrap_admin_password
            ),
            bootstrap_admin_display_name=values.get(
                "AETP_MASTER_BOOTSTRAP_ADMIN_DISPLAY_NAME",
                cls.bootstrap_admin_display_name,
            ),
            env_file=path,
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
        requested = (
            Path(env_file).resolve() if env_file is not None else None
        )
        if requested is not None and requested != _settings.env_file:
            raise RuntimeError(
                f"配置已初始化（{_settings.env_file}），"
                f"不能再用 {requested} 重新初始化"
            )
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
        raise RuntimeError(
            "配置未初始化：请在入口（run.py）先调用 master.config.configure()"
        )
    return _settings


def reset_settings() -> None:
    """重置配置（测试用）。"""
    global _settings
    _settings = None
    logger.debug("配置已重置")
