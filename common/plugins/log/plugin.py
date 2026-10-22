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
import sqlite3
from pathlib import Path
from typing import Any, ClassVar, cast

from cordis_port import Context
from cordis_port.logger import Exporter, Logger, Message
from starlette.requests import Request

from common.plugins.comm import CommMeta

from .config import LogConfig
from .export import LEVELS, RotatingFile, call_site, console_exporter, file_exporter
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
    #: 只声明 appconfig（读 ``[log]`` 节）：webapi / comm / webui 都是**延迟
    #: 挂载**（见 :meth:`_late_setup`）——本插件要足够早装载以捕获装配期
    #: 日志，而那三个服务尚未就绪；硬声明会要么死锁顺序、要么丢启动日志
    inject: ClassVar[list[str]] = ["appconfig"]
    Config: ClassVar[type[LogConfig]] = LogConfig

    def __init__(self, ctx: Context, config: LogConfig | None = None) -> None:
        self.ctx: Context = ctx
        self.config: LogConfig = config if isinstance(config, LogConfig) else LogConfig()
        self._store: LogStore | None = None
        self._trim_task: asyncio.Task[None] | None = None
        self._late_task: asyncio.Task[None] | None = None
        self._dispose_health: object = None
        self._ui_registered: bool = False
        #: 入库时间戳被钳制的条数（时钟漂移可观测）
        self._clamped: int = 0
        #: 存储写入失败次数（库损坏/不可写；出口降级不炸业务）
        self._store_failures: int = 0

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
            self._trim_task.add_done_callback(self._log_task_exit)

        if self.config.report:
            self.ctx.logger.exporter(self._sink_exporter(report_level))

        # 查询 API / 远端入库 / 查看页：依赖的 webapi / comm / webui 晚于本插件
        # 就绪（预装阶段才装），延迟挂载（无头部署自然放弃）
        self._late_task = asyncio.create_task(self._late_setup())
        self._late_task.add_done_callback(self._log_task_exit)
        self.ctx.logger.info("日志插件已加载：%s", self._store.path if self._store else "未启用查询库")

        def cleanup() -> None:
            dispose = self._dispose_health
            if callable(dispose):
                dispose()
            for task in (self._trim_task, self._late_task):
                if task is not None:
                    task.cancel()

        return cleanup

    def _log_task_exit(self, task: asyncio.Task[None]) -> None:
        """后台任务观察哨：意外退出记 error 日志，不再无声消失。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("日志插件后台任务意外退出：%s", error)

    async def _late_setup(self) -> None:
        """等 webapi / comm 就绪后挂查询 API、远端入库、健康指标与查看页。

        本插件早装（捕获装配期日志），这几个服务晚就绪（预装阶段）——
        轮询等待，就绪即挂；无头部署 30 秒后自然放弃。
        """
        for _attempt in range(60):
            api = self.ctx.get("webapi")
            comm = self.ctx.get("comm")
            if api is not None and comm is not None:
                api.get("/api/logs", self._logs_handler, kind="api", name="log.query")
                api.get("/api/logs/nodes", self._nodes_handler, kind="api", name="log.nodes")
                api.delete("/api/logs", self._delete_logs, kind="api", name="log.delete")
                self._dispose_health = api.add_health_provider("log", self._metrics)
                comm.on("log.batch", self._on_log_batch)
                break
            await asyncio.sleep(0.5)
        # 查看页常驻守护：webui 被换装 / 重载会连带清掉贡献清单（如 dist 重建后
        # ensure_installed 换装），循环校验并自动补注册；无头部署自然空转
        while True:
            webui = self.ctx.get("webui")
            if webui is not None:
                known = {item.id for item in webui.ui_contributions()}
                if "log:logs" not in known:
                    webui.register_ui(
                        "logs",
                        kind="page",
                        format="vue",
                        resource_root=str(Path(__file__).resolve().parent / "ui"),
                        entry="LogsPage.vue",
                        path="/logs",
                        title="日志",
                        menu_group="",
                        menu_order=100,
                    )
            await asyncio.sleep(5.0)

    def _metrics(self) -> dict[str, object]:
        return {
            "store": str(self._store.path) if self._store else "",
            "clamped": self._clamped,
            "store_failures": self._store_failures,
        }

    def _note_store_failure(self, exc: Exception) -> None:
        """存储不可写：出口降级（丢入库）但绝不炸业务调用；库损坏已隔离重建。"""
        self._store_failures += 1
        if self._store_failures == 1:
            self.ctx.logger.warning("日志查询库写入失败（后续静默计数）：%s", exc)

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
                "source": call_site(),
            }
            if self._store is not None:
                try:
                    clamped: list[int] = []
                    self._store.write_batch(self._node_name(), [entry], clamped=clamped)
                    self._clamped += len(clamped)
                except (sqlite3.Error, OSError) as exc:
                    self._note_store_failure(exc)
            # comm 延迟就绪：未就绪时仅本地入库，就绪后自动开始上报
            comm = self.ctx.get("comm")
            if comm is not None:
                comm.report("log.batch", {"logs": [entry]})

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
        try:
            clamped: list[int] = []
            self._store.write_batch(meta["node"], entries, clamped=clamped)
            self._clamped += len(clamped)
        except (sqlite3.Error, OSError) as exc:
            self._note_store_failure(exc)

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
        if self._store is None:
            return {"nodes": []}
        try:
            return {"nodes": self._store.nodes()}
        except (sqlite3.Error, OSError) as exc:
            return cast(dict[str, object], self.ctx.webapi.json(
                {"error": f"查询库不可用：{type(exc).__name__}"}, status_code=503
            ))

    async def _delete_logs(self, request: Request) -> dict[str, object]:
        """``DELETE /api/logs``：按当前筛选删除（审计留痕，防灭迹）。"""
        if self._store is None:
            return {"deleted": 0}
        params = request.query_params

        def as_int(raw: str | None) -> int | None:
            return int(raw) if raw and raw.isdigit() else None

        def as_float(raw: str | None) -> float | None:
            try:
                return float(raw) if raw else None
            except ValueError:
                return None

        node = params.get("node") or None
        level = as_int(params.get("level"))
        q = params.get("q") or None
        try:
            deleted = self._store.delete(
                node=node,
                level=level,
                q=q,
                from_ts=as_float(params.get("from_ts")),
                to_ts=as_float(params.get("to_ts")),
            )
        except (sqlite3.Error, OSError) as exc:
            return cast(dict[str, object], self.ctx.webapi.json(
                {"error": f"删除失败：{type(exc).__name__}"}, status_code=503
            ))
        # 删除操作本身留审计（条件 + 条数）：日志可删，删除行为不隐匿
        self.ctx.logger.info(
            "审计：删除日志 %d 条（node=%s level=%s q=%s）",
            deleted,
            node or "*",
            level if level is not None else "*",
            q or "*",
        )
        return {"deleted": deleted}


__all__: list[str] = ["LogPlugin"]
