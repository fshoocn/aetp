"""日志出口实现：控制台 + 轮转文件（由 :class:`LogPlugin` 挂载到 ``ctx.logger``）。

轮转文件按「每条一开一关」写：日志量小的管理面场景开销可接受，且不留
常驻句柄（Windows 上常驻句柄会挡住文件替换 / 临时目录清理——与元数据
存储的短连接是同款取舍）。
"""

from __future__ import annotations

import inspect
import sys
import time
from pathlib import Path

from cordis_port.logger import Exporter, Logger, LoggerLevel, Message

#: 配置级别名 → 日志级别数值（数值越大越详细；低于阈值的丢弃）
LEVELS: dict[str, int] = {
    "error": LoggerLevel.ERROR,
    "warn": LoggerLevel.WARN,
    "info": LoggerLevel.INFO,
    "debug": LoggerLevel.DEBUG,
}


#: 溯源时跳过的栈帧（框架 / 日志自身），第一个业务帧即日志调用点
_SKIP_MARKERS: tuple[str, ...] = (
    "cordis_port",
    "logging",
    "plugins/log",
    "plugins/webapi/router",
    "asyncio",
)


def call_site() -> str:
    """业务调用点 ``文件:行号``（跳过框架 / 日志栈帧；取不到返回空串）。"""
    for frame in inspect.stack()[1:]:
        filename = frame.filename.replace("\\", "/")
        if any(marker in filename for marker in _SKIP_MARKERS):
            continue
        return f"{Path(filename).name}:{frame.lineno}"
    return ""


def format_line(message: Message, text: str, source: str = "") -> str:
    """单行日志：时间戳 + 级别 + 日志名 + 调用点 + 正文。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(message.ts / 1000))
    site = f" [{source}]" if source else ""
    return f"{stamp} {message.type.upper():5s} [{message.name}]{site} {text}"


class RotatingFile:
    """极简轮转文件写入器：每条一开一关，写前超限则轮转（``.1`` 最新）。"""

    def __init__(self, path: Path, max_bytes: int, backup_count: int) -> None:
        self.path = path
        self._max_bytes = max(1, max_bytes)
        self._backup_count = max(0, backup_count)
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, line: str) -> None:
        self._rotate_if_needed()
        with open(self.path, "ab") as handle:
            handle.write((line + "\n").encode("utf-8"))

    def _rotate_if_needed(self) -> None:
        if not self.path.is_file() or self.path.stat().st_size < self._max_bytes:
            return
        if self._backup_count == 0:
            self.path.unlink(missing_ok=True)
            return
        self.path.with_name(f"{self.path.name}.{self._backup_count}").unlink(
            missing_ok=True
        )
        for index in range(self._backup_count - 1, 0, -1):
            self.path.with_name(f"{self.path.name}.{index}").replace(
                self.path.with_name(f"{self.path.name}.{index + 1}")
            )
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))


def console_exporter(level: int) -> Exporter:
    """控制台出口：error / warn 走 stderr，其余走 stdout（着色）。"""
    exporter: Exporter = {"colors": 2, "levels": {"default": level}}

    def export(message: Message) -> None:
        text = Logger.format(exporter, message)
        stream = sys.stderr if message.level <= LoggerLevel.WARN else sys.stdout
        stream.write(format_line(message, text, call_site()) + "\n")
        stream.flush()

    exporter["export"] = export
    return exporter


def file_exporter(level: int, sink: RotatingFile) -> Exporter:
    """轮转文件出口：无色纯文本（便于 grep / 归档）。"""
    exporter: Exporter = {"colors": False, "levels": {"default": level}}

    def export(message: Message) -> None:
        try:
            sink.write(format_line(message, Logger.format(exporter, message), call_site()))
        except OSError as exc:
            # 日志出口自身的 IO 失败不能拖垮业务调用；报到 stderr 即止
            sys.stderr.write(f"日志写入失败：{sink.path}：{exc}\n")

    exporter["export"] = export
    return exporter


__all__: list[str] = [
    "LEVELS",
    "RotatingFile",
    "call_site",
    "console_exporter",
    "file_exporter",
    "format_line",
]
