"""主从通信服务（``ctx.comm``）：QoS 状态机 + 持久化 outbox + cordis 事件派发。

架构（``docs/comm通信方案-2026-10-22.md``）：业务面只依赖
:class:`~common.plugins.comm.interface.CommInterface`；Core 承担投递状态机
（至少成功 / 至少重试 / 至多发送）、批量与背压、持久化 outbox（决策 A1/A3：
经 db 插件、成功即删、重启续传）、广播展开与下行 outbox、cordis 事件派发
（``comm/{topic}``）与 relay；传输经 :class:`CommTransport`（默认 HTTP）。

投递语义要点：

* ``min_success`` **任意一次成功即可**（决策 A4）：广播展开后任一目标
  ack 即整条 DONE 并撤销其余；定向 / report 即"该目标至少成功 1 次"；
* ``max_attempts`` 是硬上限（决策 D2），4xx 协议错误直接 FAILED 不吃保底
  （决策 D3）；
* at-least-once ≠ exactly-once：ack 丢失会重投，处理器需容忍重复。
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast

from cordis_port import Context, Fiber, Service
from cordis_port.events import EventDispatchError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import CommConfig
from .interface import (
    CommMeta,
    CommTopicHandler,
    DeliveryPolicy,
    Envelope,
    MessageState,
    MessageStatus,
    TransportResult,
    resolve_policy,
)
from .outbox import CommOutbox, OutboxStore
from .transport import HttpTransport

#: 单条消息上限 / 单批上限（超出拒收 / 拆批，防单条打爆链路）
MAX_ITEM_BYTES: int = 64 * 1024
MAX_BATCH_BYTES: int = 256 * 1024
#: 终态状态缓存上限（``status()`` 查询终态消息用）
FINAL_CACHE_MAX: int = 512


@dataclass
class _Record:
    """一条投递记录（定向 / report 一条；广播扇出后每目标一条，共享 group）。"""

    id: str
    group_id: str
    scope: str  # local（本节点派发）| up（上行至汇聚点）| down（汇聚点下行至节点）
    kind: str  # report | direct | broadcast
    dest: str
    topic: str
    payload: Any
    ts: float
    ttl: float  # 0 = 永不过期
    min_success: int
    min_retries: int
    max_attempts: int
    immediate: bool = False
    attempts: int = 0
    acks: int = 0
    state: str = MessageState.PENDING
    last_error: str = ""
    created_ts: float = 0.0
    updated_ts: float = 0.0
    next_at: float = 0.0
    in_flight_deadline: float = 0.0
    saved: bool = False
    save_failed: bool = False
    save_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def envelope(self, sender: str) -> Envelope:
        """构造线上信封。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "from": sender,
            "to": self.dest,
            "topic": self.topic,
            "payload": self.payload,
            "ts": self.ts,
            "ttl": self.ttl,
        }

    def row_values(self) -> dict[str, Any]:
        """持久化行值（payload 转 JSON 文本）。"""
        return {
            "id": self.id,
            "group_id": self.group_id,
            "scope": self.scope,
            "kind": self.kind,
            "dest": self.dest,
            "topic": self.topic,
            "payload": json.dumps(self.payload, ensure_ascii=False),
            "ts": self.ts,
            "ttl": self.ttl,
            "min_success": self.min_success,
            "min_retries": self.min_retries,
            "max_attempts": self.max_attempts,
            "attempts": self.attempts,
            "acks": self.acks,
            "state": self.state,
            "last_error": self.last_error,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
        }


def _record_from_row(row: dict[str, Any]) -> _Record:
    """从持久化行恢复记录（payload 反序列化，坏行降级为空对象）。"""
    try:
        payload: Any = json.loads(str(row.get("payload") or "null"))
    except (ValueError, RecursionError):
        payload = None
    return _Record(
        id=str(row["id"]),
        group_id=str(row.get("group_id") or row["id"]),
        scope=str(row.get("scope") or "up"),
        kind=str(row.get("kind") or "report"),
        dest=str(row.get("dest") or ""),
        topic=str(row.get("topic") or ""),
        payload=payload,
        ts=float(row.get("ts") or 0.0),
        ttl=float(row.get("ttl") or 0.0),
        min_success=int(row.get("min_success") or 1),
        min_retries=int(row.get("min_retries") or 0),
        max_attempts=int(row.get("max_attempts") or 0),
        attempts=int(row.get("attempts") or 0),
        acks=int(row.get("acks") or 0),
        state=str(row.get("state") or MessageState.PENDING),
        last_error=str(row.get("last_error") or ""),
        created_ts=float(row.get("created_ts") or 0.0),
        updated_ts=float(row.get("updated_ts") or 0.0),
    )


class CommService(Service[CommConfig]):
    """主从通信服务（``provide = "comm"``，实现 ``CommInterface``）。"""

    provide: str | None = "comm"
    name: str = "comm"
    #: ingest / poll / ack 挂 webapi；outbox 经 db（db 是数据库操作唯一入口）
    inject: ClassVar[list[str]] = ["appconfig", "db", "webapi"]
    Config: type[CommConfig] = CommConfig

    def __init__(self, ctx: Context, config: CommConfig | None = None) -> None:
        # ⚠️ 必须最先调用：这一步完成 ctx.comm 的注册
        super().__init__(ctx)
        self._provided_ctx: Context = ctx
        self.config: CommConfig = config if isinstance(config, CommConfig) else CommConfig()
        self._target: str = self.config.target.strip().rstrip("/")
        #: 主从通信令牌：复用 [web] auth_token（不新增密钥体系）
        self._token: str = str(self.ctx.appconfig.get("web", "auth_token") or "")
        self._transport: HttpTransport | None = (
            HttpTransport(self._target, self._token, self._node_name) if self._target else None
        )
        #: 投递记录（entry id → 记录；insertion 序 = 丢最旧顺序）
        self._records: OrderedDict[str, _Record] = OrderedDict()
        #: 终态缓存（group id → 状态，供 status() 查询）
        self._final: OrderedDict[str, MessageStatus] = OrderedDict()
        #: 汇聚点视角的节点最后活跃时间（扇出目标发现）
        self._nodes: dict[str, float] = {}
        self._wake: asyncio.Event = asyncio.Event()
        self._poll_ready: asyncio.Event = asyncio.Event()
        self._pump_task: asyncio.Task[None] | None = None
        self._pull_task: asyncio.Task[None] | None = None
        self._store: OutboxStore | None = None
        self._disk_count: int = 0
        self._bg: set[asyncio.Task[None]] = set()
        #: 管道自监控（/healthz 指标）
        self._sent: int = 0
        self._dispatched: int = 0
        self._dropped: int = 0
        self._retries: int = 0
        self._rejected: int = 0
        self._acked: int = 0
        self._failed: int = 0
        self._relays: dict[str, int] = {"out": 0, "in": 0}

    # ------------------------------------------------------------- 生命周期
    async def init(self) -> Callable[[], None]:
        """建 outbox 表 / 恢复未终结消息，挂路由与泵；返回卸载清理函数。

        db 未就绪时**显式失败**（决策 A3：不静默降级为内存——"至少投递
        1 次"的承诺不能悄悄降级）。
        """
        await self.ctx.db.sync_schema(models=[CommOutbox])
        self._store = OutboxStore(self.ctx.db)
        await self._recover()
        if 0 < self.config.default_max_attempts <= self.config.default_min_retries:
            self.ctx.logger.warning(
                "[comm] default_min_retries(%d) 与 default_max_attempts(%d) 冲突，"
                "按硬上限优先（决策 D2）：至多发送 %d 次",
                self.config.default_min_retries,
                self.config.default_max_attempts,
                self.config.default_max_attempts,
            )

        api = self.ctx.webapi
        api.post("/api/comm/ingest", self._ingest, kind="api", name="comm.ingest")
        api.post("/api/comm/poll", self._poll, kind="api", name="comm.poll")
        api.post("/api/comm/ack", self._ack, kind="api", name="comm.ack")
        dispose_health = api.add_health_provider("comm", self._metrics)

        self._pump_task = asyncio.create_task(self._pump())
        self._pump_task.add_done_callback(self._log_task_exit)
        if self._transport is not None:
            self._pull_task = asyncio.create_task(self._pull_loop())
            self._pull_task.add_done_callback(self._log_task_exit)
            self.ctx.logger.info("comm 已启动：上报 %s", self._target)
        else:
            self.ctx.logger.info("comm 已启动：本地自环（本节点即汇聚点）")
            if str(getattr(self.ctx, "node_kind", "")) != "master":
                self.ctx.logger.warning(
                    "[comm] 未配置 target：本节点消息只走本地自环，**不会上报主节点**"
                    "（从节点请在 [comm] target 填汇聚点基地址）"
                )

        def cleanup() -> Any:
            dispose_health()
            for task in (self._pump_task, self._pull_task):
                if task is not None:
                    task.cancel()

            async def finish() -> None:
                # 后台持久化任务必须在 db 排空连接池前收干净：
                # 否则任务在关停后新开连接，Windows 上文件句柄不释放
                pending: set[asyncio.Task[None]] = set(self._bg)
                for rec in self._records.values():
                    if rec.save_task is not None:
                        pending.add(rec.save_task)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                transport = self._transport
                if transport is not None:
                    await transport.close()

            return finish()

        return cleanup

    def _log_task_exit(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("comm 后台任务意外退出：%s", error)

    async def _recover(self) -> None:
        """启动恢复：未终结行重新入队续传，ttl 过期直接清理（§9.1）。"""
        assert self._store is not None
        try:
            rows = await self._store.load_all()
        except Exception as exc:
            self.ctx.logger.error("comm outbox 恢复失败：%s", exc)
            raise
        now = time.time()
        self._disk_count = len(rows)
        for row in rows:
            rec = _record_from_row(row)
            if rec.ttl > 0 and now - rec.created_ts >= rec.ttl:
                self._disk_count -= 1
                self._spawn(self._store.remove(rec.id))
                self._failed += 1
                continue
            if rec.state == MessageState.DONE:
                self._disk_count -= 1
                self._spawn(self._store.remove(rec.id))
                continue
            # 中断的 in-flight 一律回退重试（at-least-once：宁重投不漏投）
            rec.state = MessageState.RETRY_WAIT
            rec.next_at = now
            rec.saved = True
            self._records[rec.id] = rec
        if rows:
            self.ctx.logger.info("comm outbox 恢复 %d 条未终结消息", len(self._records))

    def _metrics(self) -> dict[str, object]:
        outbox: dict[str, int] = {}
        for rec in self._records.values():
            if rec.scope == "down" and rec.state in (
                MessageState.PENDING,
                MessageState.RETRY_WAIT,
                MessageState.IN_FLIGHT,
            ):
                outbox[rec.dest] = outbox.get(rec.dest, 0) + 1
        return {
            "target": self._target or "(loopback)",
            "queue": len(self._records),
            "sent": self._sent,
            "dispatched": self._dispatched,
            "dropped": self._dropped,
            "retries": self._retries,
            "rejected": self._rejected,
            "acked": self._acked,
            "failed": self._failed,
            "outbox": outbox,
            "outbox_disk": self._disk_count,
            "relays": dict(self._relays),
        }

    def _node_name(self) -> str:
        """本节点名（上报元数据）；未提供 node_name 服务时回退 unknown。"""
        try:
            return str(self.ctx.node_name)
        except AttributeError:
            return "unknown"

    def _default_policy(self) -> DeliveryPolicy:
        return resolve_policy(
            {
                "min_success": self.config.default_min_success,
                "min_retries": self.config.default_min_retries,
                "max_attempts": self.config.default_max_attempts,
            },
            default={"min_success": 1, "min_retries": 0, "max_attempts": 0},
        )

    # --------------------------------------------------------------- 发送侧
    def report(
        self,
        topic: str,
        payload: Any,
        *,
        policy: DeliveryPolicy | None = None,
        ttl: float | None = None,
        immediate: bool = False,
    ) -> str:
        """上行到汇聚点；本节点即汇聚点时本地自环。返回 message_id。"""
        problem = self._check(topic, payload)
        if problem is not None:
            self._reject(problem, topic)
            return ""
        pol = resolve_policy(policy, default=self._default_policy())
        ttl_v = self.config.broadcast_ttl if ttl is None else float(ttl)
        if self._transport is None:
            rec = self._make_record(
                scope="local", kind="report", dest=self._node_name(),
                topic=topic, payload=payload, pol=pol, ttl=ttl_v, immediate=immediate,
            )
        else:
            rec = self._make_record(
                scope="up", kind="report", dest="",
                topic=topic, payload=payload, pol=pol, ttl=ttl_v, immediate=immediate,
            )
        return self._enqueue([rec])

    def send_to(
        self,
        node: str,
        topic: str,
        payload: Any,
        *,
        policy: DeliveryPolicy | None = None,
        ttl: float | None = None,
        immediate: bool = False,
    ) -> str:
        """定向到指定节点。返回 message_id。"""
        dest = str(node or "").strip()
        problem = self._check(topic, payload)
        if problem is not None or not dest:
            self._reject(problem or "缺少目标节点", topic)
            return ""
        pol = resolve_policy(policy, default=self._default_policy())
        ttl_v = self.config.broadcast_ttl if ttl is None else float(ttl)
        if dest == self._node_name():
            rec = self._make_record(
                scope="local", kind="direct", dest=dest,
                topic=topic, payload=payload, pol=pol, ttl=ttl_v, immediate=immediate,
            )
        elif self._transport is None:
            rec = self._make_record(
                scope="down", kind="direct", dest=dest,
                topic=topic, payload=payload, pol=pol, ttl=ttl_v, immediate=immediate,
            )
        else:
            # 从节点发给其他节点：先上行至汇聚点，由其转入目标 outbox
            rec = self._make_record(
                scope="up", kind="direct", dest=dest,
                topic=topic, payload=payload, pol=pol, ttl=ttl_v, immediate=immediate,
            )
        return self._enqueue([rec])

    def broadcast(
        self,
        topic: str,
        payload: Any,
        *,
        policy: DeliveryPolicy | None = None,
        ttl: float | None = None,
        immediate: bool = False,
    ) -> str:
        """广播到全部节点（含本节点本地派发）；任一目标成功即达标（A4）。"""
        problem = self._check(topic, payload)
        if problem is not None:
            self._reject(problem, topic)
            return ""
        if self._transport is not None:
            # 非汇聚点无扇出能力：降级为上行并告警（决策 D4）
            self.ctx.logger.warning(
                "[comm] broadcast 在非汇聚点调用已降级为 report（topic=%s）", topic
            )
            return self.report(
                topic, payload, policy=policy, ttl=ttl, immediate=immediate
            )
        pol = resolve_policy(policy, default=self._default_policy())
        ttl_v = self.config.broadcast_ttl if ttl is None else float(ttl)
        group_id = uuid.uuid4().hex
        remotes = [
            name for name in self._nodes
            if name and name != self._node_name()
        ]
        records: list[_Record] = []
        for name in remotes:
            records.append(
                self._make_record(
                    scope="down", kind="broadcast", dest=name,
                    topic=topic, payload=payload, pol=pol, ttl=ttl_v,
                    immediate=immediate, group_id=group_id,
                )
            )
        if records:
            # 有远端目标：本地监听器照常收（fire-and-forget 副本，不计入成功数）
            local = self._make_record(
                scope="local", kind="broadcast", dest=self._node_name(),
                topic=topic, payload=payload,
                pol={"min_success": 0, "max_attempts": 1}, ttl=ttl_v,
                immediate=immediate,
            )
            records.append(local)
        else:
            # 无远端目标（单节点部署 / 尚无节点注册）：本地派发即"发出去"，
            # 计入成功数——"任意一次成功即可"（A4）
            records.append(
                self._make_record(
                    scope="local", kind="broadcast", dest=self._node_name(),
                    topic=topic, payload=payload, pol=pol, ttl=ttl_v,
                    immediate=immediate, group_id=group_id,
                )
            )
        self._enqueue(records)
        return group_id

    def on(self, topic: str, handler: CommTopicHandler) -> Callable[[], None]:
        """注册 topic 处理器（绑调用方 fiber，自动注销）。

        与 ``ctx.on(f"comm/{topic}", handler)`` 等价：派发统一走 cordis
        事件总线（fiber 解绑 / 上下文隔离由总线提供）。
        """
        name = str(topic or "").strip()
        if not name:
            raise ValueError("comm.on 需要非空 topic")
        dispose = self._provided_ctx.on(f"comm/{name}", handler)
        fiber = self._caller_fiber()
        if fiber is not None:
            fiber.effect(lambda: dispose, f"ctx.comm.on({name!r})")
        return dispose

    def relay(
        self,
        event: str,
        *,
        direction: str = "out",
        mode: str = "broadcast",
        node: str | None = None,
        topic: str | None = None,
    ) -> Callable[[], None]:
        """跨节点事件转发（§6.2）：``out`` 本地事件 → 网络；``in`` 网络 → 本地。

        线上 topic 约定 ``relay/{event}``（决策 D5）；注册绑调用方 fiber。
        """
        name = str(event or "").strip()
        if not name:
            raise ValueError("comm.relay 需要非空事件名")
        wire = topic or f"relay/{name}"

        if direction == "out":
            def listener(payload: Any, *_rest: Any) -> None:
                try:
                    json.dumps(payload, ensure_ascii=False)
                except (TypeError, ValueError) as exc:
                    self.ctx.logger.warning("[comm] relay %s 载荷不可序列化，拒发：%s", name, exc)
                    return
                self._relays["out"] += 1
                if mode == "broadcast":
                    self.broadcast(wire, payload)
                elif node:
                    self.send_to(node, wire, payload)
                else:
                    self.report(wire, payload)

            return self._provided_ctx.on(name, listener)

        def handler(payload: Any, meta: CommMeta) -> None:
            self._relays["in"] += 1
            self._provided_ctx.emit(name, payload)

        return self.on(wire, handler)

    def nodes(self) -> list[str]:
        """本节点已知的全部节点名称（汇聚点 = 所有接入节点 + 自身）。"""
        names = set(self._nodes)
        names.add(self._node_name())
        names.discard("")
        return sorted(names)

    def status(self, message_id: str) -> MessageStatus | None:
        """单条消息投递状态；未知 id 返回 ``None``。"""
        mid = str(message_id or "").strip()
        if not mid:
            return None
        rec = self._records.get(mid)
        gid = rec.group_id if rec is not None else mid
        group = self._group_records(gid)
        if group:
            r0 = group[0]
            states = {r.state for r in group}
            live = (
                MessageState.IN_FLIGHT
                if MessageState.IN_FLIGHT in states
                else MessageState.RETRY_WAIT
                if MessageState.RETRY_WAIT in states
                else MessageState.PENDING
            )
            return {
                "id": gid,
                "kind": r0.kind,
                "dest": r0.dest if len(group) == 1 else "*",
                "topic": r0.topic,
                "state": str(live),
                "attempts": sum(r.attempts for r in group),
                "acks": sum(r.acks for r in group),
                "last_error": next((r.last_error for r in group if r.last_error), ""),
                "created_ts": r0.created_ts,
                "updated_ts": max(r.updated_ts for r in group),
            }
        return self._final.get(gid)

    async def flush(self, timeout: float = 5.0) -> None:
        """排空在途消息（含下行 outbox）；超时返回（测试 / 关停用）。"""
        deadline = time.monotonic() + timeout
        while self._records and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

    # ------------------------------------------------------------ 记录构造
    def _check(self, topic: str, payload: Any) -> str | None:
        """校验入参；合法返回 ``None``，否则返回拒绝原因。"""
        if not topic or not topic.strip():
            return "非法 topic"
        try:
            text = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError, RecursionError) as exc:
            return f"载荷不可序列化：{exc}"
        if len(text.encode("utf-8")) > MAX_ITEM_BYTES:
            return "单条消息超过 64 KiB"
        return None

    def _reject(self, reason: str, topic: str) -> None:
        self._rejected += 1
        self.ctx.logger.warning("comm 发送拒收（topic=%r）：%s", topic, reason)

    def _make_record(
        self,
        *,
        scope: str,
        kind: str,
        dest: str,
        topic: str,
        payload: Any,
        pol: DeliveryPolicy,
        ttl: float,
        immediate: bool,
        group_id: str | None = None,
    ) -> _Record:
        now = time.time()
        rid = uuid.uuid4().hex
        return _Record(
            id=rid,
            group_id=group_id or rid,
            scope=scope,
            kind=kind,
            dest=dest,
            topic=topic.strip(),
            payload=payload,
            ts=now,
            ttl=max(0.0, ttl),
            min_success=int(pol.get("min_success", 1)),
            min_retries=int(pol.get("min_retries", 0)),
            max_attempts=int(pol.get("max_attempts", 0)),
            immediate=immediate,
            created_ts=now,
            updated_ts=now,
        )

    def _enqueue(self, records: list[_Record]) -> str:
        """入队并唤醒泵；返回组 id。背压：超限丢最旧并计数。"""
        if not records:
            return ""
        for rec in records:
            while len(self._records) >= max(1, self.config.queue_max):
                _old_id, old = self._records.popitem(last=False)
                self._dropped += 1
                self._drop_row(old)
                self.ctx.logger.warning(
                    "comm 在途消息超限，丢弃最旧一条（topic=%s）", old.topic
                )
            self._records[rec.id] = rec
            if rec.min_success > 0 and self._store is not None:
                rec.save_task = asyncio.create_task(self._save_row(rec))
            if rec.scope == "down":
                self._poll_ready.set()
        self._wake.set()
        return records[0].group_id

    # ------------------------------------------------------ 组 / 终态记账
    def _group_records(self, group_id: str) -> list[_Record]:
        return [r for r in self._records.values() if r.group_id == group_id]

    def _register_ack(self, rec: _Record) -> None:
        """一次成功投递记账；``acks >= min_success`` 整组 DONE（A4）。"""
        if rec.state in (MessageState.DONE, MessageState.FAILED):
            return
        rec.acks += 1
        rec.updated_ts = time.time()
        group = self._group_records(rec.group_id)
        if sum(r.acks for r in group) >= rec.min_success:
            self._finish_group(rec.group_id, done=True)
            return
        # 还需更多成功（min_success > 1）：该记录继续排队再投
        rec.state = MessageState.PENDING
        rec.next_at = time.time()
        self._patch_row(rec, "acks", "state", "updated_ts")

    def _fail_entry(self, rec: _Record, reason: str) -> None:
        """单条投递记录终结为 FAILED；全组终结且未达标时整组 FAILED。"""
        if rec.state in (MessageState.DONE, MessageState.FAILED):
            return
        rec.state = MessageState.FAILED
        rec.last_error = reason
        rec.updated_ts = time.time()
        self._patch_row(rec, "state", "last_error", "updated_ts")
        group = self._group_records(rec.group_id)
        terminal = all(
            r.state in (MessageState.DONE, MessageState.FAILED) for r in group
        )
        if terminal and sum(r.acks for r in group) < rec.min_success:
            self._finish_group(rec.group_id, done=False)

    def _finish_group(self, group_id: str, *, done: bool) -> None:
        """整组终结：记终态、清记录与持久化行。"""
        group = self._group_records(group_id)
        if not group:
            return
        now = time.time()
        r0 = group[0]
        status: MessageStatus = {
            "id": group_id,
            "kind": r0.kind,
            "dest": r0.dest if len(group) == 1 else "*",
            "topic": r0.topic,
            "state": str(MessageState.DONE if done else MessageState.FAILED),
            "attempts": sum(r.attempts for r in group),
            "acks": sum(r.acks for r in group),
            "last_error": next((r.last_error for r in group if r.last_error), ""),
            "created_ts": r0.created_ts,
            "updated_ts": now,
        }
        self._final[group_id] = status
        while len(self._final) > FINAL_CACHE_MAX:
            self._final.popitem(last=False)
        for rec in group:
            self._records.pop(rec.id, None)
            self._drop_row(rec)
        if done:
            self._acked += 1
        else:
            self._failed += 1

    def _failure(self, rec: _Record, error_kind: str, message: str) -> None:
        """一次投递失败的裁决：协议错误不吃保底（D3）；否则退避重试 / 放弃。"""
        if rec.state in (MessageState.DONE, MessageState.FAILED):
            return
        rec.last_error = message
        rec.updated_ts = time.time()
        if error_kind == "protocol":
            self._fail_entry(rec, message)
            return
        self._retries += 1
        retries_done = rec.attempts - rec.acks
        # 硬上限无条件优先（决策 D2）：attempts 达 max_attempts 即放弃；
        # min_retries 只在 max_attempts 允许的窗口内起保底作用（冲突时启动告警）
        give_up = rec.max_attempts > 0 and rec.attempts >= rec.max_attempts
        if give_up:
            self._fail_entry(
                rec,
                f"已达发送上限（{rec.attempts} 次，重试 {retries_done} 次）：{message}",
            )
            return
        rec.state = MessageState.RETRY_WAIT
        upper = min(self.config.retry_base * (2**rec.attempts), self.config.retry_cap)
        wait = self.config.retry_base + (upper - self.config.retry_base) * (
            secrets.randbelow(1000) / 1000.0
        )
        rec.next_at = time.time() + wait
        self._patch_row(rec, "state", "last_error", "attempts", "updated_ts")

    # -------------------------------------------------------- 持久化（A1/A3）
    async def _save_row(self, rec: _Record) -> None:
        store = self._store
        if store is None:
            return
        try:
            await store.save(rec.row_values())
            rec.saved = True
            self._disk_count += 1
        except Exception as exc:  # noqa: BLE001 —— 降级为内存并显式报出
            rec.save_failed = True
            self.ctx.logger.error("comm outbox 写盘失败（%s）：%s", rec.id, exc)
        finally:
            rec.save_task = None

    def _patch_row(self, rec: _Record, *columns: str) -> None:
        if self._store is None or not rec.saved:
            return
        values = rec.row_values()
        self._spawn(self._store.patch(rec.id, {c: values[c] for c in columns}))

    def _drop_row(self, rec: _Record) -> None:
        if self._store is None or not rec.saved:
            return
        rec.saved = False
        self._disk_count = max(0, self._disk_count - 1)
        self._spawn(self._store.remove(rec.id))

    def _spawn(self, coro: Any) -> None:
        """后台任务（持久化 / 清理）：异常进日志，不炸调用方。"""
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg_done)

    def _bg_done(self, task: asyncio.Task[None]) -> None:
        self._bg.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("comm 后台任务失败：%s", error)

    # --------------------------------------------------------------- 批量泵
    async def _pump(self) -> None:
        while True:
            try:
                await self._pump_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 泵不许死
                self.ctx.logger.error("comm 批量泵异常：%s", exc)
                await asyncio.sleep(0.5)

    async def _pump_once(self) -> None:
        self._maintenance()
        self._persist_pending()
        ready = self._ready_records()
        if not ready:
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 1.0)
            return
        if any(rec.immediate for rec in ready):
            # 即时通道（心跳等）：不攒批立即发，独立批次保低延迟
            await self._deliver([rec for rec in ready if rec.immediate])
            return
        # 攒批：够 batch_size 立即发；否则等到 batch_interval 到点
        deadline = time.monotonic() + max(0.05, self.config.batch_interval)
        while True:
            ready = self._ready_records()
            if len(ready) >= self.config.batch_size:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), remaining)
        batch: list[_Record] = []
        payload_bytes = 0
        for rec in self._ready_records():
            if len(batch) >= self.config.batch_size:
                break
            size = len(json.dumps(rec.payload, ensure_ascii=False).encode("utf-8"))
            if batch and payload_bytes + size > MAX_BATCH_BYTES:
                break
            batch.append(rec)
            payload_bytes += size
        if batch:
            await self._deliver(batch)

    def _ready_records(self) -> list[_Record]:
        now = time.time()
        return [
            rec
            for rec in self._records.values()
            if rec.scope in ("local", "up")
            and rec.state in (MessageState.PENDING, MessageState.RETRY_WAIT)
            and rec.next_at <= now
        ]

    def _persist_pending(self) -> None:
        """入队即写盘：为未落盘记录发起保存任务（min_success=0 不落盘）。"""
        if self._store is None:
            return
        for rec in list(self._records.values()):
            if (
                rec.saved
                or rec.save_failed
                or rec.save_task is not None
                or rec.min_success <= 0
            ):
                continue
            rec.save_task = asyncio.create_task(self._save_row(rec))

    async def _deliver(self, batch: list[_Record]) -> None:
        """投递一批：local 直接派发；up 经传输驱动送汇聚点。"""
        locals_ = [rec for rec in batch if rec.scope == "local"]
        ups = [rec for rec in batch if rec.scope == "up"]
        for rec in locals_:
            rec.attempts += 1
            meta: CommMeta = {
                "node": self._node_name(),
                "topic": rec.topic,
                "ts": rec.ts,
                "received_ts": time.time(),
                "id": rec.id,
            }
            await self._dispatch_payload(rec.topic, rec.payload, meta)
            self._dispatched += 1
            self._register_ack(rec)
        if not ups:
            return
        # 落盘先于投递：崩溃窗口内宁重投不漏投（at-least-once）
        for rec in ups:
            if rec.save_task is not None:
                await rec.save_task
        for rec in ups:
            rec.attempts += 1
            self._patch_row(rec, "attempts", "updated_ts")
        transport = self._transport
        if transport is None:  # pragma: no cover —— up 只在有 target 时创建
            return
        result: TransportResult = await transport.deliver(
            [rec.envelope(self._node_name()) for rec in ups]
        )
        if result["ok"]:
            self._sent += 1
            for rec in ups:
                self._register_ack(rec)
            return
        if result["error_kind"] == "protocol":
            self._dropped += len(ups)
            for rec in ups:
                self._fail_entry(rec, "被目标拒绝（协议错误，不重试）")
            return
        for rec in ups:
            self._failure(rec, "network", "投递失败（网络 / 5xx / 429）")

    async def _dispatch_payload(
        self, topic: str, payload: Any, meta: CommMeta
    ) -> None:
        """经 cordis 事件总线派发（``comm/{topic}``）；单个处理器异常隔离。"""
        try:
            await self._provided_ctx.parallel(f"comm/{topic}", payload, meta)
        except EventDispatchError as exc:
            for error in exc.errors:
                self.ctx.logger.error("comm 话题 %s 处理器异常：%s", topic, error)
        except Exception as exc:  # noqa: BLE001 —— 派发不许炸调用链
            self.ctx.logger.error("comm 话题 %s 派发异常：%s", topic, exc)

    def _spawn_dispatch(
        self, sender: str, topic: str, payload: Any, ts: float, gid: str
    ) -> None:
        async def run() -> None:
            meta: CommMeta = {
                "node": sender,
                "topic": topic,
                "ts": ts,
                "received_ts": time.time(),
                "id": gid,
            }
            await self._dispatch_payload(topic, payload, meta)
            self._dispatched += 1

        self._spawn(run())

    # ------------------------------------------------------ 下行 outbox（汇聚点）
    def _take_down(self, node: str, limit: int) -> list[Envelope]:
        """把待投下行记录交给轮询节点（记一次尝试，进入 in-flight）。"""
        now = time.time()
        items: list[Envelope] = []
        for rec in self._records.values():
            if len(items) >= limit:
                break
            if rec.scope != "down" or rec.dest != node:
                continue
            if rec.state not in (MessageState.PENDING, MessageState.RETRY_WAIT):
                continue
            if rec.next_at > now:
                continue
            rec.attempts += 1
            rec.state = MessageState.IN_FLIGHT
            rec.in_flight_deadline = now + self.config.ack_timeout
            rec.updated_ts = now
            self._patch_row(rec, "attempts", "state", "updated_ts")
            items.append(rec.envelope(self._node_name()))
        return items

    def _maintenance(self) -> None:
        """过期维护：ack 超时重投 / 放弃；ttl 兜底整组 FAILED。"""
        now = time.time()
        expired_groups: set[str] = set()
        for rec in list(self._records.values()):
            if (
                rec.scope == "down"
                and rec.state == MessageState.IN_FLIGHT
                and now >= rec.in_flight_deadline
            ):
                self._failure(rec, "network", "等待 ack 超时")
            if rec.ttl > 0 and now - rec.created_ts >= rec.ttl:
                expired_groups.add(rec.group_id)
        for gid in expired_groups:
            group = self._group_records(gid)
            if group and sum(r.acks for r in group) < group[0].min_success:
                for rec in group:
                    self._fail_entry(rec, "ttl 过期")
        if any(
            rec.scope == "down"
            and rec.state in (MessageState.PENDING, MessageState.RETRY_WAIT)
            and rec.next_at <= now
            for rec in self._records.values()
        ):
            self._poll_ready.set()

    # ------------------------------------------------------------- 传输侧（下行）
    async def _pull_loop(self) -> None:
        """从汇聚点拉取下行消息 → 本地派发 → ack（at-least-once 兜底靠超时重投）。"""
        transport = self._transport
        assert transport is not None
        while True:
            items = await transport.pull(max(1.0, self.config.poll_hang))
            if not items:
                await asyncio.sleep(0.2)  # 长轮询挂起后仍空：稍歇防热循环
                continue
            ids: list[str] = []
            for env in items:
                self._spawn_dispatch(
                    str(env.get("from") or "unknown"),
                    str(env.get("topic") or ""),
                    env.get("payload"),
                    float(env.get("ts") or time.time()),
                    str(env.get("id") or ""),
                )
                ids.append(str(env.get("id") or ""))
            await transport.ack(ids)

    # --------------------------------------------------------------- 接收侧
    async def _ingest(self, request: Request) -> dict[str, object] | JSONResponse:
        """汇聚点收件：report / 发给本节点 → 本地派发；direct → 目标 outbox。"""
        body, error = await self._read_body(request)
        if error is not None:
            return self.ctx.webapi.json({"error": error[0]}, status_code=error[1])
        items = body.get("items")
        if not isinstance(items, list):
            return self.ctx.webapi.json({"error": "缺少 items 列表"}, status_code=400)
        sender = str(
            body.get("node") or request.headers.get("x-aetp-node") or "unknown"
        )
        self._nodes[sender] = time.time()
        accepted = 0
        errors = 0
        now = time.time()
        for raw in cast(list[object], items):
            if not isinstance(raw, dict):
                errors += 1
                continue
            env = cast(dict[str, object], raw)
            gid = str(env.get("id") or uuid.uuid4().hex)
            kind = str(env.get("kind") or "report")
            dest = str(env.get("to") or "")
            topic = str(env.get("topic") or "")
            ts = float(cast(float, env.get("ts") or now))
            ttl = float(cast(float, env.get("ttl") or 0.0))
            if dest and dest not in ("", self._node_name()):
                # 定向给其他节点：转入其下行 outbox（默认策略 + 信封 ttl）
                if gid in self._records:
                    accepted += 1  # 幂等：上行重投的重复件跳过
                    continue
                self._enqueue([
                    self._make_record(
                        scope="down",
                        kind="direct" if kind != "broadcast" else "broadcast",
                        dest=dest, topic=topic, payload=env.get("payload"),
                        pol=self._default_policy(), ttl=ttl, immediate=False,
                        group_id=gid,
                    )
                ])
            else:
                self._spawn_dispatch(sender, topic, env.get("payload"), ts, gid)
            accepted += 1
        return {"accepted": accepted, "errors": errors}

    async def _poll(self, request: Request) -> dict[str, object] | JSONResponse:
        """下行长轮询：有件即返；挂起 ``poll_hang`` 秒等待新件。"""
        body, error = await self._read_body(request)
        if error is not None:
            return self.ctx.webapi.json({"error": error[0]}, status_code=error[1])
        node = str(body.get("node") or "").strip()
        if not node:
            return self.ctx.webapi.json({"error": "缺少 node"}, status_code=400)
        hang_raw = body.get("hang")
        hang = self.config.poll_hang if hang_raw is None else float(cast(float, hang_raw))
        hang = min(max(hang, 0.0), 60.0)
        limit_raw = body.get("max")
        limit = 100 if limit_raw is None else int(cast(int, limit_raw))
        limit = min(max(limit, 1), 500)
        self._nodes[node] = time.time()
        deadline = time.monotonic() + hang
        while True:
            items = self._take_down(node, limit)
            if items:
                return {"items": items}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"items": []}
            self._poll_ready.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._poll_ready.wait(), min(remaining, 1.0))

    async def _ack(self, request: Request) -> dict[str, object] | JSONResponse:
        """下行确认：成功投递记账（任一目标成功即整组达标，A4）。"""
        body, error = await self._read_body(request)
        if error is not None:
            return self.ctx.webapi.json({"error": error[0]}, status_code=error[1])
        ids = body.get("ids")
        if not isinstance(ids, list):
            return self.ctx.webapi.json({"error": "缺少 ids 列表"}, status_code=400)
        acked = 0
        for raw in cast(list[object], ids):
            rid = str(raw or "")
            rec = self._records.get(rid)
            if rec is None or rec.scope != "down":
                continue
            self._register_ack(rec)
            acked += 1
        return {"acked": acked}

    async def _read_body(
        self, request: Request
    ) -> tuple[dict[str, object], tuple[str, int] | None]:
        """解析请求体（gzip + JSON）；失败返回 ``(错误信息, 状态码)``。"""
        raw = await request.body()
        if len(raw) > MAX_BATCH_BYTES:
            return {}, ("单批超过 256 KiB", 413)
        if request.headers.get("content-encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                return {}, ("gzip 解压失败", 400)
        try:
            data: object = json.loads(raw)
        except (ValueError, RecursionError):
            return {}, ("请求体不是合法 JSON", 400)
        if not isinstance(data, dict):
            return {}, ("请求体必须是对象", 400)
        return cast(dict[str, object], data), None

    def _caller_fiber(self) -> Fiber | None:
        """调用方 fiber（``on`` / ``relay`` 注册绑生命周期用）。"""
        ctx = self.ctx
        if ctx is self._provided_ctx:
            return None
        fiber = getattr(ctx, "fiber", None)
        if fiber is None or fiber is self._provided_ctx.fiber:
            return None
        return fiber


__all__: list[str] = [
    "MAX_BATCH_BYTES",
    "MAX_ITEM_BYTES",
    "CommMeta",
    "CommService",
    "CommTopicHandler",
]
