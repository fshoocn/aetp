"""webapi 插件配置模型（监听地址、日志与插件安装目录）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

#: 字段类型表：字段名 -> (类型元组, 说明)。用于 ``validate`` 的逐项检查。
_FIELDS: dict[str, tuple[tuple[type[object], ...], str]] = {
    "host": ((str,), "监听地址"),
    "port": ((int,), "监听端口"),
    "access_log": ((bool,), "是否打印访问日志"),
    "log_level": ((str,), "uvicorn 日志级别"),
    "start_timeout": ((int, float), "启动等待秒数"),
    "cors_origins": ((list, tuple), "允许的跨源来源（CORS）"),
}

#: 允许的日志级别
_LOG_LEVELS: tuple[str, ...] = ("critical", "error", "warning", "info", "debug", "trace")


class WebApiConfig:
    """webapi 插件配置。

    类属性即默认值，可直接读取::

        WebApiConfig.port      # 8080

    实例字段与类属性同名；节点配置由 ``WebApiPlugin`` 从 ``config.ini`` 的
    ``[web]`` 节读取。
    """

    # -- 默认值（类属性当默认值，实例可覆盖） --------------------------------
    host: str = "127.0.0.1"
    port: int = 8080
    access_log: bool = False
    log_level: str = "warning"
    start_timeout: float = 10.0
    #: 允许的跨源来源：``("*",)`` 表示不限（内网测试平台取向）；具体列表则回显
    #: 对应 ``Origin``；空元组/列表 = 关闭 CORS。前端「切换后端地址」依赖此项。
    cors_origins: tuple[str, ...] = ("*",)

    def __init__(self, **values: object) -> None:
        """按字段表填充配置；未给出的字段取类属性默认值。"""
        for name in _FIELDS:
            setattr(self, name, values.get(name, getattr(type(self), name)))

    # -- 标准 Schema 协议 -----------------------------------------------------
    @classmethod
    def validate(
        cls, value: object
    ) -> dict[str, WebApiConfig] | dict[str, list[dict[str, str | list[str]]]]:
        """校验并归一化配置（标准 Schema 的 ``validate``）。

        参数:
            value: 原始配置，允许 ``None`` / 字典 / 本类实例。

        返回:
            ``{"value": WebApiConfig}``；校验失败时返回 ``{"issues": [...]}``，
            cordis 会据此抛出 ``ValidationError``。
        """
        if value is None:
            return {"value": cls()}
        config = value if isinstance(value, cls) else None
        if config is not None:
            value = config.to_dict()
        if not isinstance(value, dict):
            return {
                "issues": [
                    {
                        "message": f"配置必须是字典或 {cls.__name__}，收到 {type(value).__name__}",
                        "path": [],
                    }
                ]
            }
        value = cast(dict[str, object], value)

        issues: list[dict[str, str | list[str]]] = []
        for name, raw in value.items():
            if name not in _FIELDS:
                issues.append(
                    {
                        "message": f"未知配置项（可选项：{', '.join(sorted(_FIELDS))}）",
                        "path": [name],
                    }
                )
                continue
            expected, label = _FIELDS[name]
            # bool 是 int 的子类，需要单独排除以免 ``port=True`` 被放行
            if isinstance(raw, bool) and bool not in expected:
                issues.append({"message": f"{label} 需要 {_describe(expected)}", "path": [name]})
                continue
            if not isinstance(raw, expected):
                issues.append({"message": f"{label} 需要 {_describe(expected)}", "path": [name]})

        if issues:
            return {"issues": issues}

        if config is None:
            config = cls(**value)
        port = config.port
        if not (0 < int(port) < 65536):
            return {
                "issues": [{"message": f"监听端口需在 1~65535 之间，收到 {port}", "path": ["port"]}]
            }
        if str(config.log_level).lower() not in _LOG_LEVELS:
            return {
                "issues": [
                    {
                        "message": f"日志级别需为 {'/'.join(_LOG_LEVELS)} 之一，"
                        f"收到 {config.log_level}",
                        "path": ["log_level"],
                    }
                ]
            }
        config.log_level = str(config.log_level).lower()
        for origin in cast(list[object] | tuple[object, ...], config.cors_origins):
            if not isinstance(origin, str) or not origin.strip():
                return {
                    "issues": [
                        {
                            "message": "cors_origins 的每项需要非空字符串",
                            "path": ["cors_origins"],
                        }
                    ]
                }
        return {"value": config}

    @classmethod
    def from_ini(cls, values: Mapping[str, str]) -> WebApiConfig:
        """从 ini 的 ``[web]`` 键值（全字符串）构建配置实例。

        数值 / 布尔 / 逗号列表就地转换；转换失败的键保留原字符串，
        交给 :meth:`validate` 报告统一的类型问题（未知键同理）。
        """
        converted: dict[str, object] = {}
        for key, raw in values.items():
            if key == "port":
                converted[key] = _to_number(int, raw)
            elif key == "access_log":
                converted[key] = _to_bool(raw)
            elif key == "start_timeout":
                converted[key] = _to_number(float, raw)
            elif key == "cors_origins":
                converted[key] = [
                    part.strip() for part in raw.split(",") if part.strip()
                ]
            else:
                converted[key] = raw

        validation = cls.validate(converted)
        if "issues" in validation:
            issues = cast(dict[str, list[dict[str, str | list[str]]]], validation)[
                "issues"
            ]
            messages = "; ".join(str(issue["message"]) for issue in issues)
            raise ValueError(f"web 配置无效：{messages}")
        return cast(dict[str, WebApiConfig], validation)["value"]

    # -- 配置合并（供 Service.__resolve_config__ 使用） -----------------------
    @classmethod
    def merge(cls, *configs: object) -> WebApiConfig:
        """按「前者为底、后者覆盖」的顺序合并多层配置。

        ``Service.__resolve_config__`` 会依次传入拦截层配置、``base``、``head``，
        这里的实现保证越靠后的层优先级越高，最终产出一个完整的配置实例。
        """
        merged: dict[str, object] = {}
        for config in configs:
            if config is None:
                continue
            if isinstance(config, cls):
                merged.update({name: getattr(config, name) for name in _FIELDS})
            elif isinstance(config, dict):
                merged.update(
                    {
                        name: item
                        for name, item in cast(dict[str, object], config).items()
                        if name in _FIELDS
                    }
                )
        return cls(**merged)

    # -- 便利方法 -------------------------------------------------------------
    def to_dict(self) -> dict[str, object]:
        """导出为字典（便于日志与自省接口）。"""
        return {name: getattr(self, name) for name in _FIELDS}

    def url(self) -> str:
        """返回形如 ``http://127.0.0.1:8080`` 的访问地址。"""
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"

    def __repr__(self) -> str:
        return f"WebApiConfig({self.host}:{self.port})"


def _describe(expected: tuple[type[object], ...]) -> str:
    """把类型元组渲染成人类可读的说明（``str | None`` 之类）。"""
    names = [t.__name__ for t in expected]
    return " 或 ".join(names)


def _to_number(converter: type[int | float], raw: str) -> object:
    """把 ini 字符串转成数值；失败保留原字符串，由 ``validate`` 报类型问题。"""
    try:
        return converter(raw.strip())
    except ValueError:
        return raw


_TRUE_WORDS: frozenset[str] = frozenset({"1", "yes", "true", "on"})
_FALSE_WORDS: frozenset[str] = frozenset({"0", "no", "false", "off"})


def _to_bool(raw: str) -> object:
    """把 ini 布尔写法转成 ``bool``；写法不合法保留原字符串。"""
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    return raw


__all__: list[str] = ["WebApiConfig"]
