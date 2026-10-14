"""webui 插件配置模型。"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from common.config_base import FieldConfig


class WebUiConfig(FieldConfig):
    """webui 插件配置。

    类属性即默认值，可直接读取::

        WebUiConfig.ui_directory

    实例字段与类属性同名；``ctx.plugin(WebUiPlugin, {...})`` 传字典即可覆盖。
    配置来源分三层（后者覆盖前者）：类属性默认值 → ``config.ini`` 的
    ``[webui]`` 节（``internal/config`` 钩子注入）→ 显式传入 / ``fiber.update``。
    """

    #: 对应 ``config.ini`` 的 ``[webui]`` 节：注入默认值，并在 ``fiber.update``
    #: 提交时把新值写回该节（``internal/commit`` 钩子）
    ini_section: ClassVar[str | None] = "webui"
    #: 字段类型表：字段名 -> (类型元组, 说明)；驱动默认值、类型校验与 ini 序列化
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "ui_directory": ((str, Path), "Vue 前端构建目录"),
    }

    ui_directory: str = str(Path(__file__).resolve().parent / "static" / "ui" / "dist")

    def __repr__(self) -> str:
        return f"WebUiConfig({self.ui_directory})"


__all__: list[str] = ["WebUiConfig"]
