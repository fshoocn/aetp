"""心跳监控：连接状态机 + 指数退避重试。

状态机（覆盖三种异常场景）::

    UNKNOWN ──发送成功──▶ ONLINE ──发送失败──▶ OFFLINE ──发送成功──▶ ONLINE
      │                     │                    │
      └──发送失败──▶ OFFLINE┘                    └──持续失败：退避重试，间隔封顶

- **主模块不在线**（启动即连不上）：首次失败进 ``OFFLINE``，此后按指数退避
  重试（``interval * 2^n``，封顶 ``max_backoff``），不空转、不刷屏。
- **主模块掉线**（运行中由 ``ONLINE`` 转 ``OFFLINE``）：立即记一次告警，
  随后同样退避重试。
- **主模块上线**（由 ``OFFLINE`` 转 ``ONLINE``）：退避清零，恢复正常心跳间隔。

状态转换回调 :attr:`on_transition` 供插件层打日志 / 通知用；:meth:`snapshot`
供 ``GET /api/heartbeat/status`` 自省。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from .config import HeartbeatConfig


class HeartbeatSendError(RuntimeError):
    """心跳发送失败（通道拒收 / 投递未成功 / 载荷不合规）。"""


#: 主循环可重试的异常面：发送层归一化错误（HeartbeatSendError ⊂ RuntimeError）
#: 之外，再兜住载荷构造 / 序列化可能抛出的网络与数值类异常。
_RETRYABLE: tuple[type[BaseException], ...] = (
    HeartbeatSendError,
    OSError,
    ValueError,
    TypeError,
)

#: 状态转换回调：``(旧状态, 新状态, 失败原因或 None)``
TransitionHook = Callable[["MasterState", "MasterState", str | None], None]

#: 配置变更回调：``(已应用 {键: (旧值, 新值)}, 被跳过 {键: 原因})``
ConfigHook = Callable[[dict[str, tuple[float, float]], dict[str, str]], None]

#: 心跳载荷构造器：返回可 JSON 序列化的载荷；可为协程（便于异步收集能力扩展）
PayloadFactory = Callable[[], dict[str, object] | Awaitable[dict[str, object]]]

#: 心跳发送器：返回主模块响应对象（无内容时空字典）
Sender = Callable[[dict[str, object]], Awaitable[dict[str, object]]]


class MasterState(StrEnum):
    """主模块连接状态。"""

    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"


@dataclass(frozen=True)
class HeartbeatSnapshot:
    """某一时刻的心跳状态快照（自省接口的返回形态）。"""

    state: MasterState
    master_url: str
    interval: float
    sent: int
    failed: int
    consecutive_failures: int
    last_success_at: float | None
    last_failure_at: float | None
    last_error: str | None
    last_rtt_ms: float | None
    next_retry_in: float | None

    def to_dict(self) -> dict[str, object]:
        """转换为 JSON 可序列化的字典。"""
        return {
            "state": self.state.value,
            "master_url": self.master_url,
            "interval": self.interval,
            "sent": self.sent,
            "failed": self.failed,
            "consecutive_failures": self.consecutive_failures,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_error": self.last_error,
            "last_rtt_ms": self.last_rtt_ms,
            "next_retry_in": self.next_retry_in,
        }


class HeartbeatMonitor:
    """心跳主循环：按状态机发送心跳、失败退避、状态转换通知。

    用法::

        monitor = HeartbeatMonitor(config, sender.send, build_payload)
        task = asyncio.create_task(monitor.run())
        ...
        task.cancel()          # 插件卸载时取消即可
    """

    def __init__(
        self,
        config: HeartbeatConfig,
        send: Sender,
        build_payload: PayloadFactory,
        *,
        master_url: str = "",
        on_transition: TransitionHook | None = None,
        on_config: ConfigHook | None = None,
    ) -> None:
        self._config = config
        self._send = send
        self._build_payload = build_payload
        self._master_url = master_url
        self._on_transition = on_transition
        self._on_config = on_config
        self._state = MasterState.UNKNOWN
        self._sent = 0
        self._failed = 0
        self._consecutive_failures = 0
        self._last_success_at: float | None = None
        self._last_failure_at: float | None = None
        self._last_error: str | None = None
        self._last_rtt_ms: float | None = None
        self._next_retry_at: float | None = None

    # -- 对外查询 -----------------------------------------------------------
    @property
    def state(self) -> MasterState:
        """当前主模块连接状态。"""
        return self._state

    def snapshot(self) -> HeartbeatSnapshot:
        """返回当前状态快照。"""
        next_retry_in: float | None = None
        if self._next_retry_at is not None:
            next_retry_in = max(0.0, self._next_retry_at - time.monotonic())
        return HeartbeatSnapshot(
            state=self._state,
            master_url=self._master_url,
            interval=self._config.interval,
            sent=self._sent,
            failed=self._failed,
            consecutive_failures=self._consecutive_failures,
            last_success_at=self._last_success_at,
            last_failure_at=self._last_failure_at,
            last_error=self._last_error,
            last_rtt_ms=self._last_rtt_ms,
            next_retry_in=next_retry_in,
        )

    # -- 主循环 -------------------------------------------------------------
    async def run(self) -> None:
        """持续发送心跳，直到被取消（``task.cancel()``）。

        每轮用 ``gather(return_exceptions=True)`` 兜住 ``beat_once`` 抛出的
        **任何**意外异常（状态钩子、载荷构造等 ``_RETRYABLE`` 之外的错误
        面），折算成一次失败继续跑 —— 监控循环不允许无声死亡；
        ``CancelledError`` 不当失败，照常向上传播。
        """
        while True:
            outcome = await asyncio.gather(self.beat_once(), return_exceptions=True)
            result = outcome[0]
            if isinstance(result, Exception):
                # 围栏：可重试面之外的意外异常折算成一次失败，循环不死
                self._note_unexpected(f"{type(result).__name__}: {result}")
            elif isinstance(result, BaseException):
                # CancelledError / KeyboardInterrupt 等：照常向上传播
                raise result
            delay = self.next_delay()
            if self._consecutive_failures:
                self._next_retry_at = time.monotonic() + delay
            await asyncio.sleep(delay)

    async def beat_once(self) -> bool:
        """发送一次心跳；返回是否成功（可重试异常不外抛，保证循环不中断）。

        成功时把主模块响应里的 ``config``（若有）应用到本地运行参数——
        心跳响应即配置同步通道，主模块改配置后最迟一个心跳周期生效。
        """
        try:
            made = self._build_payload()
            payload: dict[str, object] = (
                await made if inspect.isawaitable(made) else made
            )
            started = time.monotonic()
            response = await self._send(payload)
        except _RETRYABLE as exc:
            self._on_failure(f"{type(exc).__name__}: {exc}")
            return False
        self._last_rtt_ms = (time.monotonic() - started) * 1000.0
        self._on_success()
        self._apply_server_config(response)
        return True

    # -- 状态迁移 -----------------------------------------------------------
    def _on_success(self) -> None:
        self._sent += 1
        self._consecutive_failures = 0
        self._last_success_at = time.time()
        self._last_error = None
        self._next_retry_at = None
        self._transition(MasterState.ONLINE)

    def _on_failure(self, message: str) -> None:
        self._count_failure(message)
        self._transition(MasterState.OFFLINE, message)

    def _count_failure(self, message: str) -> None:
        """记失败账（计数、时间、原因），不碰状态迁移与钩子。"""
        self._failed += 1
        self._consecutive_failures += 1
        self._last_failure_at = time.time()
        self._last_error = message

    def _note_unexpected(self, message: str) -> None:
        """异常围栏专用：记账并迁移 ``OFFLINE``（HB-N1）。

        钩子调用在 :meth:`_transition` 内单独兜底——不再整体跳过迁移，
        异常围栏路径的状态转换恢复可观测（历史 / 日志照常）。
        """
        self._count_failure(message)
        self._transition(MasterState.OFFLINE, message)

    def _transition(self, target: MasterState, error: str | None = None) -> None:
        if self._state == target:
            return
        previous, self._state = self._state, target
        if self._on_transition is not None:
            # 钩子单独兜底（HB-N1）：钩子自身异常不允许打断心跳主流程
            with contextlib.suppress(Exception):
                self._on_transition(previous, target, error)

    def _apply_server_config(self, response: dict[str, object]) -> None:
        """应用响应里的 ``config`` 字段；非法值就地跳过，不影响心跳。"""
        raw = response.get("config")
        if not isinstance(raw, dict):
            return
        # isinstance 收窄裸 dict 得 dict[Unknown, Unknown]，显式 cast 定型
        entries = cast(dict[object, object], raw)
        values = {str(key): value for key, value in entries.items()}
        applied, skipped = self._config.apply_server(values)
        if self._on_config is not None and (applied or skipped):
            self._on_config(applied, skipped)

    # -- 退避 ---------------------------------------------------------------
    def next_delay(self) -> float:
        """下一次心跳前的等待秒数：正常 ``interval``，失败后指数退避并封顶。

        连续失败第 n 次后为 ``min(interval * 2**(n-1), max_backoff)``。
        指数取 ``2.0`` 浮点幂且把指数封顶在 1023：``2.0**1024`` 会抛
        ``OverflowError``，主模块长时间宕机（上千次连续失败）不该炸掉
        心跳循环；封顶后的结果早已被 ``max_backoff`` 夹住。
        """
        if self._consecutive_failures == 0:
            return self._config.interval
        exponent = min(self._consecutive_failures - 1, 1023)
        backoff = self._config.interval * 2.0**exponent
        return min(backoff, self._config.max_backoff)
