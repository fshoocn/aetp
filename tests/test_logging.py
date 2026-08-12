"""共享日志配置测试。"""

from __future__ import annotations

import logging

from common.logging_config import configure_logging


def test_common_logging_writes_utf8_file(tmp_path):
    """共享日志配置应创建文件并写入格式化日志。"""
    log_path = configure_logging(tmp_path / "logs" / "test.log", console=False)
    logger = logging.getLogger("tests.logging")
    logger.info("日志配置测试")

    for handler in logging.getLogger().handlers:
        handler.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "tests.logging" in content
    assert "日志配置测试" in content
