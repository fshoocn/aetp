"""日志插件：真实出口（console / 轮转文件）+ 查询库 + 上报（LOG-A1 / 二期）。

**分层澄清**：业务代码照常 ``ctx.logger.info(...)``，零改动——本插件经
cordis **exporter 机制**收集日志（三个平级出口）：

* **console**：error / warn 走 stderr，其余走 stdout；
* **轮转文件**：兜底逃生通道（日志系统自身故障时 ``tail``/``grep`` 还能用）；
* **入库 / 上报**（``report = true`` 且 ``report_level`` 阈值内）：写本地
  ``log.sqlite``（查询真相源）并经 ``ctx.comm`` 发往汇聚点（``[comm] target``
  留空 = 本节点即汇聚点）。

汇聚点额外接收远端 ``log.batch`` 落**同一**本地库；``GET /api/logs``
游标查询（节点 / 级别 / 关键字 / 前后 id），``GET /api/logs/nodes`` 节点清单。
体验层（深链 / 前后文 / 操作 id / 尾随 / 导出）与任务输出流见
``docs/日志方案-2026-10-19.md``。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar, cast

from cordis_port import Context
from cordis_port.logger import Exporter, Logger, Message
from starlette.requests import Request

from common.plugins.comm import CommMeta

from .config import LogConfig
from .export import LEVELS, RotatingFile, console_exporter, file_exporter
from .op import current_operation
from .store import LogStore


def _level_value(name: str, label: str) -> int:
    """配置级别名 → 数值；非法显式报错（不静默回退）。"""
    key = str(name).strip().lower()
    if key not in LEVELS:
        raise ValueError(f"配置 {label} 取值非法：{name!r}（可选 debug / info / warn / error）")
    return LEVELS[key]


def _base_dir(ctx: Context) -> Path:
    """节点目录（config.ini 所在目录）：相对路径的解析基准。"""
    loaded = ctx.appconfig.path_loaded
    return loaded.parent if loaded is not None else Path.cwd()


class LogPlugin:
    """日志插件（提供任务输出流接口见日志方案 §5.6，随任务下发落地）。"""

    #: 插件名（即插件 id）
    name: ClassVar[str] = "log"
    display_name: ClassVar[str] = "日志服务"
    version: ClassVar[str] = "1.0.0"
    #: appconfig 读 ``[log]`` 节；webapi 挂查询 API；comm 走上报 / 远端入库
    inject: ClassVar[list[str]] = ["appconfig", "webapi", "comm"]
    Config: ClassVar[type[LogConfig]] = LogConfig

    def __init__(self, ctx: Context, config: LogConfig | None = None) -> None:
        self.ctx: Context = ctx
        self.config: LogConfig = config if isinstance(config, LogConfig) else LogConfig()
        self._store: LogStore | None = None
        self._trim_task: asyncio.Task[None] | None = None
        #: 入库时间戳被钳制的条数（时钟漂移可观测）
        self._clamped: int = 0

    # ------------------------------------------------------------- 生命周期
    def init(self) -> object:
        """挂三个出口 / 查询 API / 远端入库 / 保留清理；返回卸载清理函数。"""
        level = _level_value(self.config.level, "log.level")
        report_level = _level_value(self.config.report_level, "log.report_level")

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

        store_raw = str(self.config.store or "").strip()
        if store_raw:
            path = Path(store_raw).expanduser()
            if not path.is_absolute():
                path = _base_dir(self.ctx) / path
            self._store = LogStore(path.resolve())
            self._store.trim(
                retain_days=self.config.retain_days,
                max_rows=self.config.retain_max_rows,
            )
            self._trim_task = asyncio.create_task(self._trim_loop())
            self._trim_task.add_done_callback(self._log_trim_exit)

        if self.config.report:
            self.ctx.logger.exporter(self._sink_exporter(report_level))

        api = self.ctx.webapi
        api.get("/api/logs", self._logs_handler, kind="api", name="log.query")
        api.get("/api/logs/nodes", self._nodes_handler, kind="api", name="log.nodes")
        dispose_health = api.add_health_provider("log", self._metrics)
        self.ctx.comm.on("log.batch", self._on_log_batch)
        # 查看页（无头部署没有 webui：软依赖，缺失即纯 API）
        webui = self.ctx.get("webui")
        if webui is not None:
            webui.register_ui(
                "logs",
                kind="page",
                format="vue",
                resource_root=str(Path(__file__).resolve().parent / "ui"),
                entry="LogsPage.vue",
                path="/logs",
                title="日志",
                menu_group="系统",
                menu_order=60,
            )
        self.ctx.logger.info("日志插件已加载：%s", self._store.path if self._store else "未启用查询库")

        def cleanup() -> None:
            dispose_health()
            task = self._trim_task
            if task is not None:
                task.cancel()

        return cleanup

    def _log_trim_exit(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("日志保留清理任务意外退出：%s", error)

    def _metrics(self) -> dict[str, object]:
        return {
            "store": str(self._store.path) if self._store else "",
            "clamped": self._clamped,
        }

    def _node_name(self) -> str:
        try:
            return str(self.ctx.node_name)
        except AttributeError:
            return "unknown"

    # ------------------------------------------------- 出口：入库 + 上报
    def _sink_exporter(self, level: int) -> Exporter:
        exporter: Exporter = {"colors": False, "levels": {"default": level}}

        def export(message: Message) -> None:
            entry = {
                "ts": message.ts / 1000.0,
                "name": message.name,
                "level": int(message.level),
                "op": current_operation() or "",
                "msg": Logger.format(exporter, message),
            }
            if self._store is not None:
                clamped: list[int] = []
                self._store.write_batch(self._node_name(), [entry], clamped=clamped)
                self._clamped += len(clamped)
            self.ctx.comm.send("log.batch", {"logs": [entry]})

        exporter["export"] = export
        return exporter

    def _on_log_batch(self, payload: Any, meta: CommMeta) -> None:
        """远端日志落本地库（汇聚点角色）；坏载荷跳过并告警。

        自环回流（发送方即本节点）跳过：本地入库已由出口直接完成，避免双写。
        """
        if self._store is None or meta["node"] == self._node_name():
            return
        if not isinstance(payload, dict):
            self.ctx.logger.warning("log.batch 载荷不是对象（node=%s）", meta["node"])
            return
        logs = cast(dict[str, object], payload).get("logs")
        if not isinstance(logs, list):
            self.ctx.logger.warning("log.batch 载荷缺 logs 列表（node=%s）", meta["node"])
            return
        entries = [
            cast(dict[str, object], item)
            for item in cast(list[object], logs)
            if isinstance(item, dict)
        ]
        clamped: list[int] = []
        self._store.write_batch(meta["node"], entries, clamped=clamped)
        self._clamped += len(clamped)

    async def _trim_loop(self) -> None:
        while True:
            await asyncio.sleep(60.0)
            if self._store is not None:
                self._store.trim(
                    retain_days=self.config.retain_days,
                    max_rows=self.config.retain_max_rows,
                )

    # ----------------------------------------------------------- 查询 API
    async def _logs_handler(self, request: Request) -> dict[str, object]:
        """``GET /api/logs``：游标查询（新 → 旧；``after_id`` 为尾随方向）。"""
        if self._store is None:
            return {"logs": [], "count": 0}
        params = request.query_params

        def as_int(raw: str | None) -> int | None:
            return int(raw) if raw and raw.isdigit() else None

        def as_float(raw: str | None) -> float | None:
            try:
                return float(raw) if raw else None
            except ValueError:
                return None

        logs = self._store.query(
            node=params.get("node") or None,
            level=as_int(params.get("level")),
            q=params.get("q") or None,
            after_id=as_int(params.get("after_id")),
            before_id=as_int(params.get("before_id")),
            from_ts=as_float(params.get("from_ts")),
            to_ts=as_float(params.get("to_ts")),
            limit=int(params.get("limit") or 200),
        )
        return {"logs": logs, "count": len(logs)}

    async def _nodes_handler(self, _request: Request) -> dict[str, object]:
        """``GET /api/logs/nodes``：上报过日志的节点清单（过滤下拉）。"""
        return {"nodes": self._store.nodes() if self._store else []}


__all__: list[str] = ["LogPlugin"]
