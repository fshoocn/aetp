"""数据库工厂：根据用户传入的连接信息自动匹配驱动并创建数据库实例。

支持三种输入形式:
1. 连接串字符串:      create_database("sqlite:///data/aetp.db")
2. DatabaseConfig:    create_database(DatabaseConfig(url="..."))
3. 配置字典:          create_database({"url": "...", "engine_kwargs": {"echo": True}})

匹配规则: 解析连接串 scheme（如 mysql / postgresql），
从 impl.REGISTRY 查找对应实现类，自动补全默认驱动并实例化。
"""

from __future__ import annotations

from typing import Any

from .database_interface import DatabaseConfig, DatabaseInterface
from .impl_registry import REGISTRY


def _detect_scheme(url: str) -> str:
    """从连接串提取 scheme（去驱动后缀并小写）。

    mysql+pymysql://... -> mysql
    """
    scheme, _, rest = url.partition("://")
    if not scheme or not rest:
        raise ValueError(f"无法解析数据库连接串: {url!r}")
    return scheme.split("+", 1)[0].lower()


def create_database(
    connection: str | DatabaseConfig | dict[str, Any],
) -> DatabaseInterface:
    """根据连接信息创建数据库实例（自动匹配驱动）。

    参数:
        connection: 连接串字符串 / DatabaseConfig / 配置字典

    返回:
        对应数据库的 DatabaseInterface 实现实例

    异常:
        ValueError: 连接串无法解析或不支持的数据库类型
    """
    if isinstance(connection, str):
        config = DatabaseConfig(url=connection)
    elif isinstance(connection, DatabaseConfig):
        config = connection
    elif isinstance(connection, dict):
        config = DatabaseConfig.from_mapping(connection)
    else:
        raise TypeError(
            f"connection 必须是 str / DatabaseConfig / dict，"
            f"实际为 {type(connection).__name__}"
        )

    url = config.build_url()
    scheme = _detect_scheme(url)

    impl_cls = REGISTRY.get(scheme)
    if impl_cls is None:
        raise ValueError(
            f"不支持的数据库类型: {scheme!r}"
            f"（支持: {', '.join(sorted(set(REGISTRY)))}）"
        )
    return impl_cls(config)
