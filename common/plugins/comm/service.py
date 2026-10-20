"""主从通信服务（``ctx.comm``）：批量队列 + 退避重试 + 本地自环的统一通道。

数据流：``send`` 入队即返回 → 后台泵按「攒 ``batch_size`` 条或
``batch_interval`` 秒」成批 → 目标是本节点（``target`` 留空）直接派发到
``on`` 处理器；否则 gzip 压缩批量 ``POST /api/comm/ingest``。失败按
``random(base, min(base·2^N, cap))`` 指数退避 + 抖动无限重试（断网重连不丢）；
4xx（协议错误）该批丢弃并计数。队列满**丢最旧**并计数（日志类数据宁丢旧
不拖垮业务）。

刻意不做（fire-and-forget 边界）：确认送达、持久 outbox、RPC。单 topic 内
按入队顺序派发；跨 topic 不保证顺序。
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import inspect
import json
import secrets
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from typing import Any, ClassVar, TypedDict, cast

from cordis_port import Context, Fiber, Service
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import CommConfig

#: 单条消息上限 / 单批上限（超出拆批或拒收，防单条打爆链路）
MAX_ITEM_BYTES: int = 64 * 1024
MAX_BATCH_BYTES: int = 256 * 1024
#: 批量上报的单次 HTTP 超时（秒）
POST_TIMEOUT: float = 10.0


class CommMeta(TypedDict):
    """派发给 topic 处理器的元数据。"""

    node: str
    topic: str
    ts: float
    received_ts: float


#: topic 处理器：同步或异步均可
CommTopicHandler = Callable[[Any, CommMeta], Any]


def _post_gzip_json(url: str, node: str, items: list[dict[str, object]], token: str) -> int:
    """压缩批量上报（阻塞，交线程执行）；返回 HTTP 状态码。

    显式空代理：内网直连不经环境代理（localhost 也可能被代理劫持）。
    """
    body = gzip.compress(
        json.dumps({"node": node, "items": items}, ensure_ascii=False).encode("utf-8")
    )
    headers = {
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
        # 节点名只随 body 传递：HTTP 头限 latin-1，中文节点名会炸请求构造
    }
    if token:
        headers["X-AETP-Token"] = token
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=POST_TIMEOUT) as response:
        return int(response.status)


class CommService(Service[CommConfig]):
    """主从通信服务（``provide = "comm"``）。"""

    provide: str | None = "comm"
    name: str = "comm"
    #: ingest 路由挂在 webapi 上；令牌取自 [web] auth_token（主从机器凭证）
    inject: ClassVar[list[str]] = ["appconfig", "webapi"]
    Config: type[CommConfig] = CommConfig

    def __init__(self, ctx: Context, config: CommConfig | None = None) -> None:
        # ⚠️ 必须最先调用：这一步完成 ctx.comm 的注册
        super().__init__(ctx)
        self._provided_ctx: Context = ctx
        self.config: CommConfig = config if isinstance(config, CommConfig) else CommConfig()
        self._target: str = self.config.target.strip().rstrip("/")
        #: 主从通信令牌：复用 [web] auth_token（不新增密钥体系）
        self._token: str = str(self.ctx.appconfig.get("web", "auth_token") or "")
        self._queue: deque[dict[str, object]] = deque(maxlen=max(1, self.config.queue_max))
        self._handlers: dict[str, list[CommTopicHandler]] = {}
        self._wake: asyncio.Event = asyncio.Event()
        self._pump_task: asyncio.Task[None] | None = None
        self._in_flight: bool = False
        #: 管道自监控（/healthz 指标）
        self._sent: int = 0
        self._dispatched: int = 0
        self._dropped: int = 0
        self._retries: int = 0
        self._rejected: int = 0

    # ------------------------------------------------------------- 生命周期
    def init(self) -> object:
        """挂 ingest 路由 / 健康指标并启动批量泵；返回卸载清理函数。"""
        api = self.ctx.webapi
        api.post("/api/comm/ingest", self._ingest, kind="api", name="comm.ingest")
        dispose_health = api.add_health_provider("comm", self._metrics)
        self._pump_task = asyncio.create_task(self._pump())
        self._pump_task.add_done_callback(self._log_pump_exit)
        if self._target:
            self.ctx.logger.info("comm 已启动：上报 %s", self._target)
        else:
            self.ctx.logger.info("comm 已启动：本地自环（本节点即汇聚点）")
            if str(getattr(self.ctx, "node_kind", "")) != "master":
                self.ctx.logger.warning(
                    "[comm] 未配置 target：本节点消息只走本地自环，**不会上报主节点**"
                    "（从节点请在 [comm] target 填主节点基地址）"
                )

        def cleanup() -> None:
            dispose_health()
            task = self._pump_task
            if task is not None:
                task.cancel()

        return cleanup

    def _log_pump_exit(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("comm 批量泵意外退出：%s", error)

    def _metrics(self) -> dict[str, object]:
        return {
            "target": self._target or "(loopback)",
            "queue": len(self._queue),
            "sent": self._sent,
            "dispatched": self._dispatched,
            "dropped": self._dropped,
            "retries": self._retries,
            "rejected": self._rejected,
        }

    def _node_name(self) -> str:
        """本节点名（上报元数据）；未提供 node_name 服务时回退 unknown。"""
        try:
            return str(self.ctx.node_name)
        except AttributeError:
            return "unknown"

    # --------------------------------------------------------------- 发送侧
    def send(self, topic: str, payload: Any) -> None:
        """入队即返回（fire-and-forget）；目标由本服务配置决定（自环 / 上报）。"""
        if not topic or not topic.strip():
            self._rejected += 1
            self.ctx.logger.warning("comm.send 忽略非法 topic：%r", topic)
            return
        try:
            json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            self._rejected += 1
            self.ctx.logger.warning("comm.send 载荷不可序列化（topic=%s）：%s", topic, exc)
            return
        if self._queue.maxlen is not None and len(self._queue) >= self._queue.maxlen:
            self._dropped += 1  # deque(maxlen) 自动丢最旧
        self._queue.append({"topic": topic, "ts": time.time(), "payload": payload})
        self._wake.set()

    def on(self, topic: str, handler: CommTopicHandler) -> Callable[[], None]:
        """注册 topic 处理器（绑调用方 fiber 生命周期），返回注销函数。"""
        handlers = self._handlers.setdefault(topic, [])
        handlers.append(handler)

        def dispose() -> None:
            with contextlib.suppress(ValueError):
                handlers.remove(handler)

        fiber = self._caller_fiber()
        if fiber is not None:
            fiber.effect(lambda: dispose, f"ctx.comm.on({topic!r})")
        return dispose

    def _caller_fiber(self) -> Fiber | None:
        ctx = self.ctx
        if ctx is self._provided_ctx:
            return None
        fiber = getattr(ctx, "fiber", None)
        if fiber is None or fiber is self._provided_ctx.fiber:
            return None
        return fiber

    async def flush(self, timeout: float = 5.0) -> None:
        """排空发送队列（含在途批次）；超时返回（force_flush 语义，测试 / 关停用）。"""
        deadline = time.monotonic() + timeout
        while (self._queue or self._in_flight) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

    # --------------------------------------------------------------- 批量泵
    async def _pump(self) -> None:
        while True:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            # 攒批：够 batch_size 立即发；否则等到 batch_interval 到点
            deadline = time.monotonic() + max(0.05, self.config.batch_interval)
            while len(self._queue) < self.config.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), remaining)
                self._wake.clear()
            batch: list[dict[str, object]] = []
            payload_bytes = 0
            while self._queue and len(batch) < self.config.batch_size:
                item = self._queue[0]
                size = len(json.dumps(item["payload"], ensure_ascii=False).encode("utf-8"))
                if batch and payload_bytes + size > MAX_BATCH_BYTES:
                    break
                batch.append(self._queue.popleft())
                payload_bytes += size
            self._in_flight = True
            try:
                await self._send_batch(batch)
            finally:
                self._in_flight = False

    async def _send_batch(self, batch: list[dict[str, object]]) -> None:
        if not batch:
            return
        if not self._target:
            await self._dispatch_items(batch, node=self._node_name(), local=True)
            return
        url = f"{self._target}/api/comm/ingest"
        attempt = 0
        while True:
            try:
                status = await asyncio.to_thread(
                    _post_gzip_json, url, self._node_name(), batch, self._token
                )
            except urllib.error.HTTPError as exc:
                status = exc.code  # 4xx/5xx 显示真相，不当网络错误重试
            except (urllib.error.URLError, OSError, ValueError) as exc:
                status = 0
                self.ctx.logger.debug("comm 上报异常（将退避重试）：%s %s", type(exc).__name__, exc)
            if 200 <= status < 300:
                self._sent += 1
                return
            # 协议错误（4xx 且非 429）重试无意义：丢该批并计数
            if 400 <= status < 500 and status != 429:
                self._dropped += len(batch)
                self.ctx.logger.warning(
                    "comm 上报被目标拒绝（HTTP %s），丢弃 %d 条", status, len(batch)
                )
                return
            # 网络失败 / 5xx / 429：指数退避 + 抖动，无限重试（断网重连不丢）
            self._retries += 1
            attempt += 1
            upper = min(self.config.retry_base * (2**attempt), self.config.retry_cap)
            # 抖动 = [base, upper] 均匀随机（无密码学诉求，取 secrets 以合规）
            wait = self.config.retry_base + (upper - self.config.retry_base) * (
                secrets.randbelow(1000) / 1000.0
            )
            await asyncio.sleep(wait)

    async def _dispatch_items(
        self, items: list[dict[str, object]], *, node: str, local: bool
    ) -> None:
        now = time.time()
        for item in items:
            topic = str(item.get("topic") or "")
            handlers = self._handlers.get(topic)
            if not handlers:
                continue
            meta: CommMeta = {
                "node": node,
                "topic": topic,
                "ts": float(cast(float, item.get("ts") or now)),
                "received_ts": now,
            }
            payload = item.get("payload")
            outcomes = await asyncio.gather(
                *(self._call(handler, payload, meta) for handler in handlers),
                return_exceptions=True,
            )
            if local:
                self._dispatched += 1
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    self.ctx.logger.error(
                        "comm 话题 %s 处理器异常：%s", topic, outcome
                    )

    async def _call(
        self, handler: CommTopicHandler, payload: Any, meta: CommMeta
    ) -> None:
        result = handler(payload, meta)
        if inspect.isawaitable(result):
            await result

    # --------------------------------------------------------------- 接收侧
    async def _ingest(self, request: Request) -> dict[str, object] | JSONResponse:
        raw = await request.body()
        if len(raw) > MAX_BATCH_BYTES:
            return self.ctx.webapi.json({"error": "单批超过 256 KiB"}, status_code=413)
        if request.headers.get("content-encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                return self.ctx.webapi.json({"error": "gzip 解压失败"}, status_code=400)
        try:
            data: object = json.loads(raw)
        except (ValueError, RecursionError):
            return self.ctx.webapi.json({"error": "请求体不是合法 JSON"}, status_code=400)
        if not isinstance(data, dict):
            return self.ctx.webapi.json({"error": "请求体必须是对象"}, status_code=400)
        body = cast(dict[str, object], data)
        raw_items = body.get("items")
        if not isinstance(raw_items, list):
            return self.ctx.webapi.json({"error": "缺少 items 列表"}, status_code=400)
        items = cast(list[object], raw_items)
        node = str(body.get("node") or request.headers.get("x-aetp-node") or "unknown")
        entries = [cast(dict[str, object], item) for item in items if isinstance(item, dict)]
        await self._dispatch_items(entries, node=node, local=False)
        return {"accepted": len(entries), "errors": max(0, len(items) - len(entries))}


__all__: list[str] = [
    "MAX_BATCH_BYTES",
    "MAX_ITEM_BYTES",
    "POST_TIMEOUT",
    "CommMeta",
    "CommService",
    "CommTopicHandler",
]
