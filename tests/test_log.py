from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from cordis_port import Context

from common.cordis_utils import unload_all
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.log import LogPlugin
from common.plugins.log.export import LEVELS, RotatingFile


class LogPluginTests(unittest.IsolatedAsyncioTestCase):
    """日志出口：文件落盘 / 级别过滤 / 轮转 / 控制台 / 非法配置。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.context: Context | None = None

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
        self._temp.cleanup()

    async def load(self, log_ini: str) -> None:
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\n\n[log]\n" + log_ini,
            encoding="utf-8",
        )
        self.context = Context()
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(LogPlugin)

    async def test_file_exporter_writes_formatted_line(self) -> None:
        """默认落盘到节点目录 data/logs/aetp.log，带时间戳/级别/名称。"""
        await self.load("console = false\n")
        assert self.context is not None
        self.context.logger.info("hello %s", "world")
        log_file = self.root / "data" / "logs" / "aetp.log"
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("hello world", text)
        self.assertIn("INFO", text)

    async def test_relative_file_anchors_to_config_dir(self) -> None:
        """相对路径按 config.ini 所在目录解析，不随 cwd 漂移。"""
        await self.load("console = false\nfile = logs/x.log\n")
        assert self.context is not None
        self.context.logger.info("anchor")
        self.assertTrue((self.root / "logs" / "x.log").is_file())

    async def test_level_filter_drops_below_threshold(self) -> None:
        """level=warn：info 丢弃、error 落盘。"""
        await self.load("console = false\nlevel = warn\n")
        assert self.context is not None
        self.context.logger.info("dropped-info")
        self.context.logger.error("kept-error")
        text = (self.root / "data" / "logs" / "aetp.log").read_text(encoding="utf-8")
        self.assertNotIn("dropped-info", text)
        self.assertIn("kept-error", text)

    async def test_empty_file_disables_file_exporter(self) -> None:
        """file 留空 = 关闭文件出口，不建目录。"""
        await self.load("console = false\nfile =\n")
        assert self.context is not None
        self.context.logger.info("only-memory")
        self.assertFalse((self.root / "data").exists())

    async def test_console_exporter_splits_streams(self) -> None:
        """控制台：info 走 stdout、error 走 stderr。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            await self.load("file =\n")
            assert self.context is not None
            self.context.logger.info("to-stdout")
            self.context.logger.error("to-stderr")
        self.assertIn("to-stdout", out.getvalue())
        self.assertNotIn("to-stdout", err.getvalue())
        self.assertIn("to-stderr", err.getvalue())

    async def test_invalid_level_is_rejected(self) -> None:
        """level 取值非法显式报错（不静默回退默认值）。"""
        with self.assertRaisesRegex(ValueError, "log.level 取值非法"):
            await self.load("console = false\nlevel = verbose\n")

    async def test_rotation_keeps_backup(self) -> None:
        """超 max_bytes 轮转：.1 备份出现，当前文件重新累积。"""
        await self.load("console = false\nfile = rot.log\nmax_bytes = 80\nbackup_count = 1\n")
        assert self.context is not None
        for index in range(6):
            self.context.logger.info("line-%d %s", index, "x" * 40)
        # file = rot.log 相对路径锚定 config.ini 所在目录（即 self.root）
        self.assertTrue((self.root / "rot.log.1").is_file())
        self.assertTrue((self.root / "rot.log").is_file())


class RotatingFileTests(unittest.TestCase):
    """轮转写入器单测：backup_count = 0 时不保留备份。"""

    def test_zero_backup_removes_oldest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sink = RotatingFile(Path(temporary) / "x.log", max_bytes=10, backup_count=0)
            for index in range(4):
                sink.write(f"row-{index}-{'y' * 20}")
            directory = Path(temporary)
            self.assertTrue((directory / "x.log").is_file())
            self.assertFalse((directory / "x.log.1").exists())

    def test_levels_map_covers_config_names(self) -> None:
        self.assertEqual(sorted(LEVELS), ["debug", "error", "info", "warn"])


if __name__ == "__main__":
    unittest.main()
