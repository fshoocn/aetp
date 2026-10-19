"""webapi 插件配置模型（监听地址、日志与插件安装目录）。
"""

from __future__ import annotations

import re
from typing import ClassVar, cast

from common.config_base import FieldConfig, Issue

#: 允许的日志级别
_LOG_LEVELS: tuple[str, ...] = ("critical", "error", "warning", "info", "debug", "trace")

#: CORS 来源格式：``*`` 或 http(s) 源（Origin 没有路径部分；尾部斜杠已归一化剥掉）
_ORIGIN: re.Pattern[str] = re.compile(r"https?://[^/\s]+")


class WebApiConfig(FieldConfig):
    """webapi 插件配置。

    类属性即默认值，可直接读取::

        WebApiConfig.port      # 8080

    实例字段与类属性同名。配置来源分三层（后者覆盖前者）：类属性默认值 →
    ``config.ini`` 的 ``[web]`` 节（``internal/config`` 钩子注入）→
    ``ctx.plugin(WebApiPlugin, {...})`` / ``fiber.update({...})`` 显式传入。
    """

    #: 对应 ``config.ini`` 的 ``[web]`` 节：注入默认值，并在 ``fiber.update``
    #: 提交时把新值写回该节（``internal/commit`` 钩子）
    ini_section: ClassVar[str | None] = "web"
    #: 字段类型表：字段名 -> (类型元组, 说明)；驱动默认值、类型校验与 ini 序列化
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "host": ((str,), "监听地址"),
        "port": ((int,), "监听端口"),
        "access_log": ((bool,), "是否打印访问日志"),
        "log_level": ((str,), "uvicorn 日志级别"),
        "start_timeout": ((int, float), "启动等待秒数"),
        "cors_origins": ((list, tuple), "允许的跨源来源（CORS）"),
    }

    # -- 默认值（类属性当默认值，实例可覆盖） --------------------------------
    host: str = "127.0.0.1"
    port: int = 8080
    access_log: bool = False
    log_level: str = "warning"
    start_timeout: float = 10.0
    #: 允许的跨源来源：``("*",)`` 表示不限（内网测试平台取向）；具体列表则回显
    #: 对应 ``Origin``；空元组/列表 = 关闭 CORS。前端「切换后端地址」依赖此项。
    cors_origins: tuple[str, ...] = ("*",)

    def _validate_extra(self) -> list[Issue]:
        """监听地址、端口范围、日志级别归一化与枚举检查、CORS 项检查。"""
        issues: list[Issue] = []
        if not self.host.strip():
            issues.append({"message": "监听地址不能为空", "path": ["host"]})
        if not (0 < int(self.port) < 65536):
            issues.append(
                {"message": f"监听端口需在 1~65535 之间，收到 {self.port}", "path": ["port"]}
            )
        self.log_level = str(self.log_level).lower()
        if self.log_level not in _LOG_LEVELS:
            issues.append(
                {
                    "message": f"日志级别需为 {'/'.join(_LOG_LEVELS)} 之一，收到 {self.log_level}",
                    "path": ["log_level"],
                }
            )
        normalized_origins: list[str] = []
        for origin in cast("list[object] | tuple[object, ...]", self.cors_origins):
            if not isinstance(origin, str) or not origin.strip():
                issues.append(
                    {"message": "cors_origins 的每项需要非空字符串", "path": ["cors_origins"]}
                )
                continue
            # 归一化（去空白与尾部斜杠）：CORSMiddleware 按字符串精确匹配，
            # 多一个斜杠就永不命中；格式不合法的项直接报错，不静默放过
            value = origin.strip().rstrip("/")
            if value != "*" and not _ORIGIN.fullmatch(value):
                issues.append(
                    {
                        "message": f"cors_origins 的每项需为 * 或 http(s) 源（如 https://host[:port]），收到 {origin!r}",
                        "path": ["cors_origins"],
                    }
                )
                continue
            normalized_origins.append(value)
        self.cors_origins = tuple(normalized_origins)
        return issues

    # -- 便利方法 -------------------------------------------------------------
    def url(self) -> str:
        """返回形如 ``http://127.0.0.1:8080`` 的访问地址。"""
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"

    def __repr__(self) -> str:
        return f"WebApiConfig({self.host}:{self.port})"


__all__: list[str] = ["WebApiConfig"]
