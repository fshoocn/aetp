"""webui 插件配置模型。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

#: 字段类型表：字段名 -> (类型元组, 说明)。用于 ``validate`` 的逐项检查。
_FIELDS: dict[str, tuple[tuple[type[object], ...], str]] = {
    "ui_directory": ((str, Path), "Vue 前端构建目录"),
}


class WebUiConfig:
    """webui 插件配置。

    类属性即默认值，可直接读取::

        WebUiConfig.ui_directory

    实例字段与类属性同名；``ctx.plugin(WebUiPlugin, {...})`` 传字典即可覆盖。
    """

    ui_directory: str = str(Path(__file__).resolve().parent / "static" / "ui" / "dist")

    def __init__(self, **values: object) -> None:
        """按字段表填充配置；未给出的字段取类属性默认值。"""
        for name in _FIELDS:
            setattr(self, name, values.get(name, getattr(type(self), name)))

    # -- 标准 Schema 协议 -----------------------------------------------------
    @classmethod
    def validate(
        cls, value: object
    ) -> dict[str, WebUiConfig] | dict[str, list[dict[str, str | list[str]]]]:
        """校验并归一化配置（标准 Schema 的 ``validate``）。

        参数:
            value: 原始配置，允许 ``None`` / 字典 / 本类实例。

        返回:
            ``{"value": WebUiConfig}``；校验失败时返回 ``{"issues": [...]}``，
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
            # bool 是 int 的子类，需要单独排除
            if isinstance(raw, bool) and bool not in expected:
                issues.append({"message": f"{label} 需要 {_describe(expected)}", "path": [name]})
                continue
            if not isinstance(raw, expected):
                issues.append({"message": f"{label} 需要 {_describe(expected)}", "path": [name]})

        if issues:
            return {"issues": issues}

        if config is None:
            config = cls(**value)
        return {"value": config}

    # -- 配置合并（供 Service.__resolve_config__ 使用） -----------------------
    @classmethod
    def merge(cls, *configs: object) -> WebUiConfig:
        """按「前者为底、后者覆盖」的顺序合并多层配置。"""
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

    def __repr__(self) -> str:
        return f"WebUiConfig({self.ui_directory})"


def _describe(expected: tuple[type[object], ...]) -> str:
    """把类型元组渲染成人类可读的说明（``str | None`` 之类）。"""
    names = [t.__name__ for t in expected]
    return " 或 ".join(names)


__all__: list[str] = ["WebUiConfig"]
