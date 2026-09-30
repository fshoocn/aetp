"""web 插件配置模型。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

#: 字段类型表：字段名 -> (类型元组, 说明)。用于 ``validate`` 的逐项检查。
_FIELDS: dict[str, tuple[tuple[type[object], ...], str]] = {
    "host": ((str,), "监听地址"),
    "port": ((int,), "监听端口"),
    "access_log": ((bool,), "是否打印访问日志"),
    "log_level": ((str,), "uvicorn 日志级别"),
    "start_timeout": ((int, float), "启动等待秒数"),
    "ui_directory": ((str, Path), "Vue 前端构建目录"),
    "plugin_install_root": ((str, Path), "插件安装目录"),
}

#: 允许的日志级别
_LOG_LEVELS: tuple[str, ...] = ("critical", "error", "warning", "info", "debug", "trace")


class WebConfig:
    """web 插件配置。

    类属性即默认值，可直接读取::

        WebConfig.port      # 8080

    实例字段与类属性同名；``ctx.plugin(WebPlugin, {...})`` 传字典即可覆盖。
    """

    # -- 默认值（类属性当默认值，实例可覆盖） --------------------------------
    host: str = "127.0.0.1"
    port: int = 8080
    access_log: bool = False
    log_level: str = "warning"
    start_timeout: float = 10.0
    ui_directory: str = str(Path(__file__).resolve().parent / "static" / "ui" / "dist")
    plugin_install_root: str = str(Path(__file__).resolve().parent.parent / "installed")

    def __init__(self, **values: object) -> None:
        """按字段表填充配置；未给出的字段取类属性默认值。"""
        for name in _FIELDS:
            setattr(self, name, values.get(name, getattr(type(self), name)))

    # -- 标准 Schema 协议 -----------------------------------------------------
    @classmethod
    def validate(
        cls, value: object
    ) -> dict[str, WebConfig] | dict[str, list[dict[str, str | list[str]]]]:
        """校验并归一化配置（标准 Schema 的 ``validate``）。

        参数:
            value: 原始配置，允许 ``None`` / 字典 / 本类实例。

        返回:
            ``{"value": WebConfig}``；校验失败时返回 ``{"issues": [...]}``，
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
        return {"value": config}

    # -- 配置合并（供 Service.__resolve_config__ 使用） -----------------------
    @classmethod
    def merge(cls, *configs: object) -> WebConfig:
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
        return f"WebConfig({self.host}:{self.port})"


def _describe(expected: tuple[type[object], ...]) -> str:
    """把类型元组渲染成人类可读的说明（``str | None`` 之类）。"""
    names = [t.__name__ for t in expected]
    return " 或 ".join(names)


__all__: list[str] = ["WebConfig"]
