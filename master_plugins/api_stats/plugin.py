"""api-stats：拦截链 ``observe`` 阶段的热点接口统计（Phase 1 / 设计 §4.5）。

作为 ``ctx.webapi.use`` 的最小完整示例（``observe`` / ``SKIP``）：

* 键用**路由模板**（``rc.route.path``）——键数 = 路由数，内存天然有界，
  不会被路径参数打散（§4.6）；
* 排除机器通道（``exclude_prefixes``：心跳 / comm 长轮询 / 健康探针）；
* **聚合后周期输出**热点 Top-N（次数 / p95 / 错误率），不逐请求写日志；
* p95 用固定桶直方图（1 / 5 / 10 / 50 / 100 / 500 / 1000 / 5000 ms），不存样本；
* ``add_health_provider`` 把累计与最近窗口挂进 ``/healthz``。

拦截器契约（``RequestContext``）由宿主 webapi 持有：插件铁律禁止 import
插件包，这里用结构化 Protocol 鸭子类型对齐（同「共享契约放公共层」的
思路；只有字段访问，无运行时依赖）。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from cordis_port import Context
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from common.hook_chain import OnError

from .config import ApiStatsConfig

#: p95 直方图桶上界（毫秒，§4.6：固定桶、不保存样本）
_BUCKET_MS: tuple[int, ...] = (1, 5, 10, 50, 100, 500, 1000, 5000)


class _RouteInfo(Protocol):
    """``RequestContext.route`` 的结构化视图（宿主持有真实类型）。"""

    path: str
    kind: str


class _RequestContext(Protocol):
    """拦截链现场的结构化视图（本插件只读 ``route`` / ``request``）。"""

    request: Request
    route: _RouteInfo


@dataclass(slots=True)
class _RouteStats:
    """单个「方法 + 路由模板」的聚合计数。"""

    count: int = 0
    errors: int = 0  # 5xx 计错误（4xx 是客户端问题，计入次数不计错误）
    buckets: list[int] = field(default_factory=lambda: [0] * (len(_BUCKET_MS) + 1))

    def record(self, status: int, milliseconds: float) -> None:
        self.count += 1
        if status >= 500:
            self.errors += 1
        for index, bound in enumerate(_BUCKET_MS):
            if milliseconds <= bound:
                self.buckets[index] += 1
                return
        self.buckets[-1] += 1

    def p95_ms(self) -> str:
        """直方图估 p95：累计到 95% 计数所在桶的上界（如 ``≤50ms``）。"""
        if self.count == 0:
            return "-"
        target = math.ceil(self.count * 0.95)
        seen = 0
        for index, bound in enumerate(_BUCKET_MS):
            seen += self.buckets[index]
            if seen >= target:
                return f"≤{bound}ms"
        return ">5000ms"

    def error_rate(self) -> str:
        return f"{self.errors / self.count:.1%}" if self.count else "-"


class ApiStatsPlugin:
    """热点接口统计：``observe`` 阶段拦截器 + 周期 Top-N + ``/healthz`` 指标。"""

    name: ClassVar[str] = "api-stats"
    display_name: ClassVar[str] = "API 热点统计"
    version: ClassVar[str] = "1.0.0"
    #: 需要 webapi（拦截链）与 appconfig（[api-stats] 节）
    inject: ClassVar[list[str]] = ["appconfig", "webapi"]
    Config: ClassVar[type[ApiStatsConfig]] = ApiStatsConfig

    def __init__(self, ctx: Context, config: ApiStatsConfig | None = None) -> None:
        self.ctx: Context = ctx
        self.config: ApiStatsConfig = (
            config if isinstance(config, ApiStatsConfig) else ApiStatsConfig()
        )
        #: 累计：(method, 路由模板) -> 聚合
        self._stats: dict[tuple[str, str], _RouteStats] = {}
        #: 最近窗口（每个输出周期清零）
        self._window: dict[tuple[str, str], _RouteStats] = {}
        self._task: asyncio.Task[None] | None = None
        self._dispose_health: object = None

    async def init(self) -> Callable[[], Awaitable[None]]:
        """登记拦截器 / 健康指标并起周期输出；返回卸载清理函数。"""
        # use() 已绑调用方 fiber（卸载即摘）；add_health_provider 只返回注销
        # 函数、不自动绑 fiber（契约不一致，§2.1），故在清理里显式注销
        self.ctx.webapi.use(
            self._observe,
            phase="observe",
            order=10,
            on_error=OnError.SKIP,
            name="api-stats",
        )
        self._dispose_health = self.ctx.webapi.add_health_provider(
            "api_stats", self._snapshot
        )
        self._task = asyncio.create_task(self._report_loop())
        self._task.add_done_callback(self._log_task_exit)
        self.ctx.logger.info(
            "api-stats 已启动：每 %ss 输出热点 Top-%d",
            self.config.log_interval,
            self.config.top_n,
        )

        async def cleanup() -> None:
            dispose = self._dispose_health
            if callable(dispose):
                dispose()
            task, self._task = self._task, None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        return cleanup

    # ------------------------------------------------------------- 拦截器
    async def _observe(
        self,
        rc: _RequestContext,
        nxt: Callable[[], Awaitable[Response]],
    ) -> Response:
        """``observe``：计时 + 按路由模板聚合（自身出错由 SKIP 放行）。"""
        path = rc.route.path
        if any(path.startswith(prefix) for prefix in self.config.exclude_prefixes):
            return await nxt()
        started = time.perf_counter()
        status = 500
        try:
            response = await nxt()
            status = response.status_code
            return response
        except HTTPException as exc:  # 4xx 也要计入
            status = exc.status_code
            raise
        finally:
            self._record(rc.request.method, path, status, time.perf_counter() - started)

    def _record(self, method: str, path: str, status: int, seconds: float) -> None:
        key = (method, path)
        milliseconds = seconds * 1000.0
        for store in (self._stats, self._window):
            row = store.setdefault(key, _RouteStats())
            row.record(status, milliseconds)

    # ------------------------------------------------------- 周期输出与指标
    def _top(
        self, store: dict[tuple[str, str], _RouteStats]
    ) -> list[dict[str, object]]:
        ranked = sorted(
            store.items(), key=lambda item: item[1].count, reverse=True
        )[: self.config.top_n]
        return [
            {
                "method": method,
                "path": path,
                "count": row.count,
                "p95": row.p95_ms(),
                "error_rate": row.error_rate(),
            }
            for (method, path), row in ranked
        ]

    def _format_top(self, entries: list[dict[str, object]]) -> str:
        if not entries:
            return "（无流量）"
        return "; ".join(
            f"{row['method']} {row['path']} ×{row['count']} p95={row['p95']} err={row['error_rate']}"
            for row in entries
        )

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.log_interval)
            top = self._top(self._window)
            self.ctx.logger.info(
                "热点接口 Top-%d：%s", self.config.top_n, self._format_top(top)
            )
            self._window.clear()

    def _snapshot(self) -> dict[str, object]:
        """``/healthz`` 的 ``metrics.api_stats``：累计 + 最近窗口 + Top-N。"""
        total = sum(row.count for row in self._stats.values())
        errors = sum(row.errors for row in self._stats.values())
        window_requests = sum(row.count for row in self._window.values())
        return {
            "routes": len(self._stats),
            "requests": total,
            "errors": errors,
            "window_requests": window_requests,
            "top": self._top(self._stats),
        }

    def _log_task_exit(self, task: asyncio.Task[None]) -> None:
        """后台任务观察哨：意外退出记 error 日志，不再无声消失。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("api-stats 后台任务意外退出：%s", error)


__all__: list[str] = ["ApiStatsPlugin"]
