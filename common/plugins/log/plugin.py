"""日志插件：真实出口（console / 轮转文件）+ 查询库 + 上报（LOG-A1 / 二期）。

**分层澄清**：业务代码照常 ``ctx.logger.info(...)``，零改动——本插件经
cordis **exporter 机制**收集日志（三个平级出口）：

* **console**：error / warn 走 stderr，其余走 stdout；
* **轮转文件**：兜底逃生通道（日志系统自身故障时 ``tail``/``grep`` 还能用）；
* **入库 / 上报**（``report = true`` 且 ``report_level`` 阈值内）：写本地
  aetp.db 的 ``aetp_log_entries`` 表（查询真相源）并经 ``ctx.comm`` 发往汇聚点（``[comm] target``
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

from cordis_port import Context, Fiber, Service
from cordis_port.logger import Exporter, Logger, Message

from common.contracts import CommMeta
from common.log_op import current_operation

from .api_service import LogApiService
from .config import LogConfig
from .export import LEVELS, RotatingFile, call_site, console_exporter, file_exporter
from .ingest_service import LogIngestService
from .store import LogEntry, LogStore
from .ui_service import LogUiService


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


class LogPlugin(Service[LogConfig]):
    """日志插件（``ctx.log``，``provide = "log"``；任务输出流接口见日志方案
    §5.6，随任务下发落地）。

    Service 类自己就是插件（开发文档 13.1 惯用法）：``ctx.log`` 即日志核心
    服务，查看页 ``LogUiService`` 以 ``inject = ["log", "webui"]`` 依赖它——
    核心没就位页面不装载，核心换装 / 重载页面随依赖纪元级联重挂。
    """

    #: 插件名（即插件 id）
    name: str = "log"
    display_name: ClassVar[str] = "日志服务"
    version: ClassVar[str] = "1.0.0"
    #: 服务名（即 ``ctx.log``）
    provide: str | None = "log"
    #: 硬声明只有 appconfig（读 ``[log]`` 节）：本插件要足够早装载以捕获装配期
    #: 日志，硬声明 comm / webapi 会要么死锁顺序、要么丢启动日志。Web / comm
    #: 侧全部拆**附属服务**分批装载，核心不沾晚到服务、也无需轮询等待：
    #: 查询 API（LogApiService，``inject = ["log", "webapi"]``）、远端入库
    #: （LogIngestService，``inject = ["log", "comm"]``）、查看页（LogUiService，
    #: ``inject = ["log", "webui"]``）——依赖就位自动装载、换装随依赖纪元重挂。
    #: 出口热路径对 comm 用 ``ctx.get("comm")`` 运行期动态取用（缺席仅本地
    #: 入库）；optional 注入是绑定期快照，晚到服务解析恒为 None，不可用作
    #: 运行期取用。
    inject: ClassVar[list[str]] = ["appconfig"]
    Config: type[LogConfig] = LogConfig

    def __init__(self, ctx: Context, config: LogConfig | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.log
        self.ctx: Context = ctx
        self.config: LogConfig = config if isinstance(config, LogConfig) else LogConfig()
        self._store: LogStore | None = None
        self._trim_task: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._api_fiber: Fiber | None = None
        self._ingest_fiber: Fiber | None = None
        self._ui_fiber: Fiber | None = None
        #: 入库缓冲（出口同步、入库异步）：(节点, 条目) 待批量写 aetp.db
        self._pending: list[tuple[str, dict[str, object]]] = []
        #: 落库串行锁：查询路径的 read-your-writes 等在途批次写完
        self._flush_lock: asyncio.Lock = asyncio.Lock()
        #: 入库时间戳被钳制的条数（时钟漂移可观测）
        self._clamped: int = 0
        #: 存储写入失败次数（库不可写；出口降级不炸业务）
        self._store_failures: int = 0

    # ------------------------------------------------------------- 生命周期
    async def init(self) -> object:
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
            path = path.resolve()
            sink = RotatingFile(path, self.config.max_bytes, self.config.backup_count)
            self.ctx.logger.exporter(file_exporter(level, sink))
            self.ctx.logger.info("日志文件：%s", path)

        # 查询库 = aetp.db 的 aetp_log_entries 表（单库设计）；db 不可用时
        # 降级为仅文件出口（不能因库故障丢掉后续日志）
        db = self.ctx.get("db", strict=False)
        if self.config.store and db is not None:
            try:
                await db.sync_schema(models=[LogEntry])
                self._store = LogStore(db)
                await self._store.trim(
                    retain_days=self.config.retain_days,
                    max_rows=self.config.retain_max_rows,
                )
                self._flush_task = asyncio.create_task(self._flush_loop())
                self._flush_task.add_done_callback(self._log_task_exit)
                self._trim_task = asyncio.create_task(self._trim_loop())
                self._trim_task.add_done_callback(self._log_task_exit)
            except Exception as exc:  # noqa: BLE001 —— 入库故障绝不阻断日志出口
                self._note_store_failure(exc)

        if self.config.report:
            self.ctx.logger.exporter(self._sink_exporter(report_level))

        # Web / comm / UI 侧拆附属服务分批装载：inject 依赖集就绪即装载，
        # 无守护轮询。fiber 挂在本插件生命周期下，卸载 / 热更自动带走。
        # 不能 await——webapi / comm / webui 晚于本插件（预装阶段才装），
        # fiber 会停在 PENDING 等依赖，await 会把装配顺序卡死。
        self._api_fiber = self.ctx.plugin(LogApiService)
        self._ingest_fiber = self.ctx.plugin(LogIngestService)
        self._ui_fiber = self.ctx.plugin(LogUiService)
        self.ctx.logger.info(
            "日志插件已加载：%s", "aetp.db:aetp_log_entries" if self._store else "未启用查询库"
        )

        async def cleanup() -> None:
            tasks = [t for t in (self._trim_task, self._flush_task) if t is not None]
            for task in tasks:
                task.cancel()
            # 取消必须**等落地**：后台任务要在 db 排空连接池前收干净，
            # 否则任务在关停后归还连接，Windows 上文件句柄不释放
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.flush_pending()  # 停机前把缓冲尽量落库

        return cleanup

    def _log_task_exit(self, task: asyncio.Task[None]) -> None:
        """后台任务观察哨：意外退出记 error 日志，不再无声消失。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("日志插件后台任务意外退出：%s", error)

    def metrics(self) -> dict[str, object]:
        """健康指标（``/healthz`` 的 ``log`` 提供方快照）。"""
        return {
            "store": "aetp_log_entries" if self._store else "",
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
            entry: dict[str, object] = {
                "ts": message.ts / 1000.0,
                "name": message.name,
                "level": int(message.level),
                "op": current_operation() or "",
                "msg": Logger.format(exporter, message),
                "source": call_site(),
            }
            if self._store is not None:
                self._enqueue(self._node_name(), entry)
            # comm 未就绪时仅本地入库，就绪后自动开始上报（ctx.get 动态解析）
            comm = self.ctx.get("comm")
            if comm is not None:
                comm.report("log.batch", {"logs": [entry]})

        exporter["export"] = export
        return exporter

    def _enqueue(self, node: str, entry: dict[str, object]) -> None:
        """入缓冲（同步上下文安全）：超上限丢最旧并计失败，防止无界增长。"""
        self._pending.append((node, entry))
        if len(self._pending) > 5000:
            self._pending.pop(0)
            self._note_store_failure(RuntimeError("入库缓冲溢出"))

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            await self.flush_pending()

    async def flush_pending(self) -> None:
        """按节点分组批量落库；失败计数降级（绝不炸业务）。

        全程持锁：查询路径的 ``flush_pending``（read-your-writes）必须等
        在途批次写完才算落库——否则后来者看到空缓冲即返回，读到的却是
        少了在途批次的旧快照（CI 慢机器上实测踩中）。
        """
        async with self._flush_lock:
            if not self._pending or self._store is None:
                return
            pending, self._pending = self._pending, []
            grouped: dict[str, list[dict[str, object]]] = {}
            for node, entry in pending:
                grouped.setdefault(node, []).append(entry)
            for node, entries in grouped.items():
                try:
                    clamped: list[int] = []
                    await self._store.write_batch(node, entries, clamped=clamped)
                    self._clamped += len(clamped)
                except Exception as exc:  # noqa: BLE001 —— 入库故障绝不阻断日志出口
                    self._note_store_failure(exc)

    def ingest_remote(self, payload: Any, meta: CommMeta) -> None:
        """远端日志入本地库（汇聚点角色）；坏载荷跳过并告警。

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
        for item in cast(list[object], logs):
            if isinstance(item, dict):
                self._enqueue(meta["node"], cast(dict[str, object], item))

    async def _trim_loop(self) -> None:
        while True:
            await asyncio.sleep(60.0)
            if self._store is not None:
                await self._store.trim(
                    retain_days=self.config.retain_days,
                    max_rows=self.config.retain_max_rows,
                )

    # ----------------------------------------------------------- 查询门面
    async def query_logs(
        self,
        *,
        node: str | None = None,
        level: int | None = None,
        q: str | None = None,
        after_id: int | None = None,
        before_id: int | None = None,
        around_id: int | None = None,
        from_ts: float | None = None,
        to_ts: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        """游标查询（新 → 旧；语义同 :meth:`LogStore.query`）；read-your-writes。"""
        await self.flush_pending()  # 查询前先落库
        if self._store is None:
            return []
        return await self._store.query(
            node=node,
            level=level,
            q=q,
            after_id=after_id,
            before_id=before_id,
            around_id=around_id,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=limit,
        )

    async def known_nodes(self) -> list[str]:
        """全部已知节点名（过滤下拉）。

        合并两个来源：日志库出现过的节点 + comm 通道已接入的节点
        （汇聚点可见所有节点，含尚未产生日志的）——下拉列**所有节点名称**，
        不受"是否有日志"限制。
        """
        await self.flush_pending()  # read-your-writes
        names: set[str] = set()
        if self._store is not None:
            names.update(await self._store.nodes())
        comm = self.ctx.get("comm")  # ctx.get 动态解析（ctx.comm 是绑定期快照，晚到恒 None）
        if comm is not None:
            try:
                names.update(comm.nodes())
            except (AttributeError, TypeError):
                pass  # 老服务无 nodes() 时静默降级为日志库来源
        return sorted(names)

    async def delete_logs(
        self,
        *,
        node: str | None = None,
        level: int | None = None,
        q: str | None = None,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> int:
        """按当前筛选删除（审计留痕，防灭迹）；返回删除行数。"""
        await self.flush_pending()  # read-your-writes：删的就是当前全部
        if self._store is None:
            return 0
        deleted = await self._store.delete(
            node=node,
            level=level,
            q=q,
            from_ts=from_ts,
            to_ts=to_ts,
        )
        # 删除操作本身留审计（条件 + 条数）：日志可删，删除行为不隐匿
        self.ctx.logger.info(
            "审计：删除日志 %d 条（node=%s level=%s q=%s）",
            deleted,
            node or "*",
            level if level is not None else "*",
            q or "*",
        )
        return deleted


__all__: list[str] = ["LogPlugin"]
