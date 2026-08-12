"""AETP 共享日志配置。

日志初始化只由组合根调用一次；业务模块使用标准 logging.getLogger(__name__)。
默认同时输出到控制台和 runtime_dir/logs/aetp.log，文件按大小滚动保留备份。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_DEFAULT_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5
_HANDLER_MARK = "_aetp_logging_handler"


def _parse_level(level: str | int) -> int:
    """将日志级别名称转换为 logging 常量。"""
    if isinstance(level, int):
        return level
    value = getattr(logging, level.strip().upper(), None)
    if not isinstance(value, int):
        raise ValueError(f"不支持的日志级别: {level!r}")
    return value


def configure_logging(
    log_file: str | Path,
    *,
    level: str | int = "INFO",
    console: bool = True,
) -> Path:
    """初始化 AETP 日志并返回日志文件绝对路径。

    函数可重复调用：相同日志文件不会重复添加 handler；配置变化时会更新
    已有 AETP handler 的级别和输出目标。
    """
    log_path = Path(log_file).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_level = _parse_level(level)
    formatter = logging.Formatter(_DEFAULT_FORMAT)
    root = logging.getLogger()
    root.setLevel(log_level)

    existing_file: RotatingFileHandler | None = None
    existing_console: logging.Handler | None = None
    for handler in list(root.handlers):
        if not getattr(handler, _HANDLER_MARK, False):
            continue
        if isinstance(handler, RotatingFileHandler):
            existing_file = handler
        elif isinstance(handler, logging.StreamHandler):
            existing_console = handler

    if existing_file is None or Path(existing_file.baseFilename).resolve() != log_path:
        if existing_file is not None:
            root.removeHandler(existing_file)
            existing_file.close()
        existing_file = RotatingFileHandler(
            log_path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        setattr(existing_file, _HANDLER_MARK, True)
        root.addHandler(existing_file)
    existing_file.setLevel(log_level)
    existing_file.setFormatter(formatter)

    if console and existing_console is None:
        existing_console = logging.StreamHandler(sys.stdout)
        setattr(existing_console, _HANDLER_MARK, True)
        root.addHandler(existing_console)
    elif not console and existing_console is not None:
        root.removeHandler(existing_console)
        existing_console.close()
        existing_console = None
    if existing_console is not None:
        existing_console.setLevel(log_level)
        existing_console.setFormatter(formatter)

    return log_path


def get_logger(name: str) -> logging.Logger:
    """获取共享日志器，便于业务模块保持统一写法。"""
    return logging.getLogger(name)
