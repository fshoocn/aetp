"""日志插件：给 ``ctx.logger`` 挂真实出口（控制台 + 轮转文件，LOG-A1）。

cordis 的 ``LoggerService`` 默认只有 1000 条内存环形缓冲，进程内回溯够用
但运行时**不可见**。本插件（主从同一份）按 ``[log]`` 配置挂两个出口：

* **console**：error / warn 走 stderr，其余走 stdout；
* **轮转文件**：超 ``max_bytes`` 轮转、留 ``backup_count`` 份，日志文件
  路径相对节点目录解析（不随启动时的工作目录漂移）。

出口经 cordis exporter 机制挂载、随本插件 fiber 卸载自动注销（热更新改
配置即换出口）。二期在本插件内追加「上报 exporter + 查询 API + 日志查看
页」，任务输出流接口见 ``docs/日志方案-2026-10-19.md`` §5.6。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from cordis_port import Context

from .config import LogConfig
from .export import LEVELS, RotatingFile, console_exporter, file_exporter


def _base_dir(ctx: Context) -> Path:
    """节点目录（config.ini 所在目录）：相对文件路径的解析基准。"""
    loaded = ctx.appconfig.path_loaded
    return loaded.parent if loaded is not None else Path.cwd()


class LogPlugin:
    """日志插件（``provide`` 无——出口挂载型，二期提供任务输出流接口）。"""

    #: 插件名（即插件 id）
    name: ClassVar[str] = "log"
    #: 展示名与版本：内置插件没有 plugin.json，清单元数据由插件类属性提供
    display_name: ClassVar[str] = "日志服务"
    version: ClassVar[str] = "1.0.0"
    #: 依赖应用配置（读 ``[log]`` 节与节点目录基准）
    inject: ClassVar[list[str]] = ["appconfig"]
    #: 配置模型（标准 Schema）：驱动默认值、类型校验与 ini 写回
    Config: ClassVar[type[LogConfig]] = LogConfig

    def __init__(self, ctx: Context, config: LogConfig | None = None) -> None:
        self.ctx: Context = ctx
        self.config: LogConfig = config if isinstance(config, LogConfig) else LogConfig()

    def init(self) -> None:
        """挂载 console / 文件出口（fiber effect 自动注销）。

        抛出:
            ValueError: ``level`` 取值非法（显式报错，不静默回退默认值）。
        """
        level_name = str(self.config.level).strip().lower()
        if level_name not in LEVELS:
            raise ValueError(
                f"配置 log.level 取值非法：{self.config.level!r}"
                "（可选 debug / info / warn / error）"
            )
        level = LEVELS[level_name]

        if self.config.console:
            self.ctx.logger.exporter(console_exporter(level))

        file_raw = str(self.config.file or "").strip()
        if file_raw:
            path = Path(file_raw).expanduser()
            if not path.is_absolute():
                path = _base_dir(self.ctx) / path
            sink = RotatingFile(
                path.resolve(), self.config.max_bytes, self.config.backup_count
            )
            self.ctx.logger.exporter(file_exporter(level, sink))
            self.ctx.logger.info("日志文件：%s", path)


__all__: list[str] = ["LogPlugin"]
