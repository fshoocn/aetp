from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cordis_port import Context
from sqlalchemy import text as sa_text

from _helpers import arequest as _arequest
from _helpers import free_port as _free_port
from common.cordis_utils import unload_all
from common.log_op import log_operation
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.comm import CommService
from common.plugins.db import DbPlugin
from common.plugins.log import LogPlugin, LogStore
from common.plugins.log.export import LEVELS, RotatingFile
from common.plugins.log.store import LogEntry
from common.plugins.webapi import WebApiPlugin


class LogPluginTests(unittest.IsolatedAsyncioTestCase):
    """日志出口：文件落盘 / 级别过滤 / 轮转 / 控制台 / 非法配置。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._temp.name)
        self.port = _free_port()
        self.context: Context | None = None

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
        self._temp.cleanup()

    async def load(self, log_ini: str) -> None:
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\n"
            "\n[web]\n"
            f"port = {self.port}\n"
            "auth_token = test-token\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'aetp.db').as_posix()}\n"
            "\n[comm]\n"
            "batch_interval = 0.05\n"
            "\n[log]\n" + log_ini,
            encoding="utf-8",
        )
        self.context = Context()
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)
        await self.context.plugin(WebApiPlugin)
        await self.context.plugin(CommService)
        await self.context.plugin(LogPlugin)

    async def test_file_exporter_writes_formatted_line(self) -> None:
        """默认落盘到节点目录 data/plugins/log/aetp.log，带时间戳/级别/名称。"""
        await self.load("console = false\n")
        assert self.context is not None
        self.context.logger.info("hello %s", "world")
        log_file = self.root / "data" / "plugins" / "log" / "aetp.log"
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
        await self.load("console = false\nlevel = warn\nstore = false\n")
        assert self.context is not None
        self.context.logger.info("dropped-info")
        self.context.logger.error("kept-error")
        text = (self.root / "data" / "plugins" / "log" / "aetp.log").read_text(encoding="utf-8")
        self.assertNotIn("dropped-info", text)
        self.assertIn("kept-error", text)

    async def test_empty_file_disables_file_exporter(self) -> None:
        """file / store 留空 = 关闭对应出口，不建目录。"""
        await self.load("console = false\nfile =\nstore = false\n")
        assert self.context is not None
        self.context.logger.info("only-memory")
        self.assertFalse((self.root / "data").exists())

    async def test_console_exporter_splits_streams(self) -> None:
        """控制台：info 走 stdout、error 走 stderr。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            await self.load("file =\nstore = false\n")
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
        await self.load(
            "console = false\nfile = rot.log\nmax_bytes = 80\nbackup_count = 1\nstore = false\n"
        )
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


class LogStoreTests(unittest.IsolatedAsyncioTestCase):
    """查询库（aetp.db 内 aetp_log_entries）：批量写 / 游标查 / 双限保留 / 钳制。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        config_path = root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(root / 'aetp.db').as_posix()}\n",
            encoding="utf-8",
        )
        self.context = Context()
        self.context.provide("node_kind", "master")
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)
        db = self.context.get("db")
        await db.sync_schema(models=[LogEntry])
        self.store = LogStore(db)

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self._temp.cleanup()

    async def test_write_query_and_trim(self) -> None:
        store = self.store
        await store.write_batch(
            "n1", [{"ts": time.time() - 20, "name": "a", "level": 2, "op": "", "msg": "m1"}]
        )
        await store.write_batch(
            "n2",
            [{"ts": time.time() - 10, "name": "b", "level": 0, "op": "", "msg": "boom-needle"}],
        )
        rows = await store.query()
        self.assertEqual([row["msg"] for row in rows], ["boom-needle", "m1"])  # 新→旧
        self.assertEqual([row["node"] for row in await store.query(node="n2")], ["n2"])
        self.assertEqual(len(await store.query(q="needle")), 1)
        self.assertEqual(len(await store.query(level=0)), 1)
        tail = await store.query(after_id=0)
        self.assertEqual([row["msg"] for row in tail], ["m1", "boom-needle"])  # 旧→新
        self.assertEqual(await store.nodes(), ["n1", "n2"])

        await store.write_batch(
            "n1",
            [{"ts": time.time() - 200000, "name": "old", "level": 2, "op": "", "msg": "ancient"}],
        )
        await store.trim(retain_days=1, max_rows=10)
        self.assertEqual([row["msg"] for row in await store.query()], ["boom-needle", "m1"])
        await store.trim(retain_days=30, max_rows=1)
        self.assertEqual([row["msg"] for row in await store.query()], ["boom-needle"])

    async def test_delete_by_filter_and_clear_all(self) -> None:
        """删除：按筛选删（返回条数），不带条件 = 清空全部。"""
        store = self.store
        await store.write_batch(
            "n1", [{"ts": time.time(), "name": "a", "level": 2, "op": "", "msg": "keep"}]
        )
        await store.write_batch(
            "n2", [{"ts": time.time(), "name": "b", "level": 2, "op": "", "msg": "drop-me"}]
        )
        self.assertEqual(await store.delete(q="drop-me"), 1)
        self.assertEqual([row["msg"] for row in await store.query()], ["keep"])
        self.assertEqual(await store.delete(), 1)
        self.assertEqual(await store.query(), [])

    async def test_keyword_matches_op_name_and_source(self) -> None:
        """关键字命中 msg / op / 来源名 / 调用点（``q=op-xxxx`` = 查看本次操作）。"""
        store = self.store
        await store.write_batch(
            "n1",
            [
                {
                    "ts": time.time(),
                    "name": "webapi",
                    "level": 2,
                    "op": "op-1a2b3c",
                    "msg": "enable done",
                    "source": "plugin.py:20",
                },
                {"ts": time.time(), "name": "heartbeat", "level": 2, "op": "", "msg": "beat"},
            ],
        )
        self.assertEqual([row["msg"] for row in await store.query(q="op-1a2b3c")], ["enable done"])
        self.assertEqual([row["msg"] for row in await store.query(q="webapi")], ["enable done"])
        self.assertEqual([row["msg"] for row in await store.query(q="plugin.py:20")], ["enable done"])
        self.assertEqual([row["msg"] for row in await store.query(q="beat")], ["beat"])
        self.assertEqual([row["msg"] for row in await store.query(q="no-such")], [])
        # 按筛选删除与查询同一套谓词：删 op 目标行，其余保留
        self.assertEqual(await store.delete(q="op-1a2b3c"), 1)
        self.assertEqual([row["msg"] for row in await store.query()], ["beat"])

    async def test_write_clamps_out_of_range_timestamps(self) -> None:
        clamped: list[int] = []
        await self.store.write_batch(
            "n",
            [{"ts": time.time() + 10**7, "name": "x", "level": 2, "op": "", "msg": "f"}],
            clamped=clamped,
        )
        self.assertEqual(len(clamped), 1)
        row = (await self.store.query())[0]
        row_ts = row["ts"]
        assert isinstance(row_ts, float)
        self.assertLessEqual(row_ts, time.time() + 25 * 3600.0)


class LogQueryTests(unittest.IsolatedAsyncioTestCase):
    """查询 API / 远端入库 / op 关联 / report_level 阈值。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._temp.name)
        self.port = _free_port()
        self.context: Context | None = None

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
        self._temp.cleanup()

    async def boot(self, *, report_level: str = "debug") -> None:
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\n"
            "\n[web]\n"
            f"port = {self.port}\n"
            "auth_token = test-token\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'aetp.db').as_posix()}\n"
            "\n[comm]\n"
            "batch_interval = 0.05\n"
            "\n[log]\n"
            "console = false\n"
            "file =\n"
            "store = true\n"
            f"report_level = {report_level}\n",
            encoding="utf-8",
        )
        self.context = Context()
        self.context.provide("node_name", "test-node")
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)
        await self.context.plugin(WebApiPlugin)
        await self.context.plugin(CommService)
        await self.context.plugin(LogPlugin)

    async def get_json(self, path: str) -> dict[str, Any]:
        assert self.context is not None
        status, body = await _arequest(f"http://127.0.0.1:{self.port}", path)
        self.assertEqual(status, 200)
        return json.loads(body)

    async def test_local_log_lands_in_store_with_op(self) -> None:
        """本地日志经出口入库（含 op 关联），查询 API 可按关键字/节点过滤。"""
        await self.boot()
        assert self.context is not None
        with log_operation() as operation:
            self.context.logger.info("启用完成 %s", "demo")
        await self.context.comm.flush(timeout=5.0)
        data = await self.get_json(f"/api/logs?q={quote('启用完成')}")
        rows = data["logs"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["node"], "test-node")
        self.assertEqual(rows[0]["op"], operation)
        self.assertIn("启用完成 demo", rows[0]["msg"])
        nodes = await self.get_json("/api/logs/nodes")
        self.assertEqual(nodes["nodes"], ["test-node"])

    async def test_nodes_lists_all_known_nodes(self) -> None:
        """节点下拉列全部已知节点：含 comm 已接入但尚无日志的节点。"""
        await self.boot()
        assert self.context is not None
        data = await self.get_json("/api/logs/nodes")
        # comm 已知节点（自身）无日志也应出现在清单里
        self.assertEqual(data["nodes"], ["test-node"])
        self.context.logger.info("有日志了")
        await self.context.comm.flush(timeout=5.0)
        data = await self.get_json("/api/logs/nodes")
        self.assertEqual(data["nodes"], ["test-node"])

    async def test_around_id_returns_context(self) -> None:
        """前后文模式（体验 B）：锚点行 + 前 / 后各约一半，统一时间轴新→旧。"""
        await self.boot()
        assert self.context is not None
        for index in range(5):
            self.context.logger.info("上下文 %d", index)
        await self.context.comm.flush(timeout=5.0)
        all_rows = (await self.get_json(f"/api/logs?q={quote('上下文')}"))["logs"]
        self.assertEqual(len(all_rows), 5)
        anchor = all_rows[2]  # 新→旧第 3 条
        data = await self.get_json(f"/api/logs?around_id={anchor['id']}&limit=5")
        rows = data["logs"]
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["id"] for row in rows], sorted((r["id"] for r in rows), reverse=True))
        self.assertEqual(rows[2]["id"], anchor["id"])
        self.assertEqual(
            [row["msg"] for row in rows],
            ["上下文 0", "上下文 1", "上下文 2", "上下文 3", "上下文 4"][::-1],
        )

    async def test_remote_batch_lands_with_source_node(self) -> None:
        """远端 log.batch 落本地库并保留来源节点（自环回流去重不双写）。"""
        await self.boot()
        assert self.context is not None
        body = json.dumps(
            {
                "node": "slave-x",
                "items": [
                    {
                        "topic": "log.batch",
                        "ts": time.time(),
                        "payload": {
                            "logs": [
                                {
                                    "ts": time.time(),
                                    "name": "remote",
                                    "level": 1,
                                    "op": "",
                                    "msg": "远端消息",
                                }
                            ]
                        },
                    }
                ],
            }
        ).encode()
        status, _ = await _arequest(
            f"http://127.0.0.1:{self.port}",
            "/api/comm/ingest",
            method="POST",
            body=body,
            content_type="application/json",
        )
        self.assertEqual(status, 200)
        data = await self.get_json(f"/api/logs?q={quote('远端消息')}")
        self.assertEqual(data["logs"][0]["node"], "slave-x")

    async def test_report_level_filters_store(self) -> None:
        """report_level 为入库/上报阈值：低于阈值不入库。"""
        await self.boot(report_level="warn")
        assert self.context is not None
        self.context.logger.info("info-不入库")
        self.context.logger.error("error-入库")
        await self.context.comm.flush(timeout=5.0)
        data = await self.get_json("/api/logs")
        messages = [row["msg"] for row in data["logs"]]
        self.assertIn("error-入库", "\n".join(messages))
        self.assertNotIn("info-不入库", "\n".join(messages))

    async def test_store_failure_never_breaks_logging(self) -> None:
        """查询表异常时出口降级不炸业务调用；表恢复后自动继续入库。"""
        await self.boot()
        assert self.context is not None
        db = self.context.get("db")
        await db.execute(sa_text("DROP TABLE aetp_log_entries"))  # 模拟库表故障
        self.context.logger.error("故障期间的日志")  # 不应抛出
        await asyncio.sleep(0.5)  # 等一轮 flush（失败即降级计数）
        await db.sync_schema(models=[LogEntry])  # 修复
        self.context.logger.error("恢复后的日志")
        await asyncio.sleep(0.5)  # 等一轮 flush 落库
        data = await self.get_json(f"/api/logs?q={quote('恢复后')}")
        self.assertEqual(len(data["logs"]), 1)

    async def test_time_window_filters(self) -> None:
        """from_ts / to_ts 时间窗过滤（体验 E 的快捷范围）。"""
        await self.boot(report_level="debug")
        assert self.context is not None
        with log_operation():
            self.context.logger.info("窗内消息")
        await self.context.comm.flush(timeout=5.0)
        recent = await self.get_json(
            f"/api/logs?q={quote('窗内')}&from_ts={time.time() - 60}"
        )
        self.assertEqual(len(recent["logs"]), 1)
        empty = await self.get_json(
            f"/api/logs?q={quote('窗内')}&to_ts={time.time() - 60}"
        )
        self.assertEqual(empty["logs"], [])


if __name__ == "__main__":
    unittest.main()
