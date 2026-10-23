"""日志插件配置模型（``[log]`` 节）。"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from common.config_base import FieldConfig


class LogConfig(FieldConfig):
    """日志插件配置。

    类属性即默认值，可直接读取 ``LogConfig.level``；实例字段与类属性同名；
    ``ctx.plugin(LogPlugin, {...})`` 传字典即可覆盖。配置来源分三层（后者
    覆盖前者）：类属性默认值 → ``config.ini`` 的 ``[log]`` 节
    （``internal/config`` 钩子注入）→ 显式传入 / ``fiber.update``。
    """

    #: 对应 ``config.ini`` 的 ``[log]`` 节：注入默认值，并在 ``fiber.update``
    #: 提交时把新值写回该节（``internal/commit`` 钩子）
    ini_section: ClassVar[str | None] = "log"
    #: 字段类型表：字段名 -> (类型元组, 说明)；驱动默认值、类型校验与 ini 序列化
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "level": ((str,), "最低日志级别：debug / info / warn / error"),
        "console": ((bool,), "控制台输出开关"),
        "file": ((str, Path), "本地轮转文件路径（留空关闭；相对节点目录解析）"),
        "max_bytes": ((int,), "单个日志文件上限（字节），超出轮转"),
        "backup_count": ((int,), "轮转保留份数（.1 最新 … .N 最旧）"),
        "store": ((str, Path), "查询库 log.sqlite 路径（留空关闭；相对节点目录解析）"),
        "report": ((bool,), "经 comm 上报/入库（留 false 则仅本地出口）"),
        "report_level": ((str,), "入库 / 上报级别阈值（可高于 level，省流量）"),
        "retain_days": ((int, float), "查询库保留天数（仅汇聚点生效）"),
        "retain_max_rows": ((int,), "查询库条数上限（仅汇聚点生效）"),
    }

    level: str = "info"
    console: bool = True
    file: str = str(Path("data") / "plugins" / "log" / "aetp.log")
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 3
    store: str = str(Path("data") / "plugins" / "log" / "log.sqlite")
    report: bool = True
    report_level: str = "info"
    retain_days: float = 7.0
    retain_max_rows: int = 500000

    def __repr__(self) -> str:
        return (
            f"LogConfig(level={self.level!r}, console={self.console!r}, file={self.file!r})"
        )


__all__: list[str] = ["LogConfig"]
