"""进程内拦截链（HookChain）：宿主拥有扩展点，贡献者经宿主服务注册。

与插件无关的小工具（放 ``common/``，各宿主复用）：

* **阶段（phase）+ 序号（order）** 决定顺序，同序再按 ``(owner, name, seq)``——
  与插件加载顺序无关（cordis 事件只有「注册序 / prepend」两种顺序，热装卸下不可控）；
* 拦截器签名 ``async (ctx, next) -> result``：不调 ``next`` 即短路（如鉴权拒绝）；
  ``next()`` 不带参数，数据经宿主定义的可变上下文 ``ctx`` 传递；
* 失败策略逐拦截器声明：``FAIL`` 向上抛（fail-closed，鉴权类），``SKIP`` 记问题后
  放行（fail-open，统计 / 日志类）。``SKIP`` 下：``next`` 之前出错 → 当它不存在继续链；
  ``next`` 之后出错 → 保留下游结果；下游自己抛的异常照常向上传播；
* 每次运行取排序快照：热装卸不影响在途请求；
* 洋葱结构下无法区分「自身耗时」与「下游耗时」，因此不做强制超时，改为统计
  **不含下游的自身耗时**，由宿主暴露 / 告警。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

type Next[R] = Callable[[], Awaitable[R]]
type Interceptor[C, R] = Callable[[C, Next[R]], Awaitable[R]]
type Terminal[C, R] = Callable[[C], Awaitable[R]]


class OnError(StrEnum):
    """拦截器自身出错时的处置。"""

    FAIL = "fail"
    SKIP = "skip"


class NextCalledTwice(RuntimeError):
    """编码错误：同一拦截器内重复调用 ``next``——任何失败策略都不吞。"""


@dataclass(frozen=True, slots=True)
class HookInfo:
    """拦截器的只读描述（自省接口 / 排序键）。"""

    phase: str
    order: int
    owner: str
    name: str
    on_error: OnError


@dataclass(slots=True)
class _Hook[C, R]:
    fn: Interceptor[C, R]
    info: HookInfo
    seq: int
    calls: int = 0
    errors: int = 0
    self_seconds: float = 0.0


@dataclass(slots=True)
class _Frame[R]:
    """单次调用的现场：下游是否已调用 / 结果 / 异常 / 耗时。"""

    called: bool = False
    has_result: bool = False
    result: R | None = None
    downstream_error: BaseException | None = None
    downstream_seconds: float = 0.0


class HookChain[C, R]:
    """按阶段与序号排序的拦截链。"""

    def __init__(
        self,
        phases: Sequence[str],
        *,
        on_problem: Callable[[str], None] | None = None,
    ) -> None:
        self._rank: dict[str, int] = {phase: index for index, phase in enumerate(phases)}
        self._hooks: list[_Hook[C, R]] = []
        self._sorted: tuple[_Hook[C, R], ...] = ()
        self._seq = 0
        self._on_problem = on_problem

    def use(
        self,
        fn: Interceptor[C, R],
        *,
        phase: str,
        owner: str,
        name: str | None = None,
        order: int = 100,
        on_error: OnError = OnError.SKIP,
    ) -> Callable[[], None]:
        """登记拦截器，返回注销函数（宿主负责把它绑到调用方 fiber 上）。"""
        if phase not in self._rank:
            raise ValueError(f"未知阶段 {phase!r}，可选：{list(self._rank)}")
        self._seq += 1
        label: str = name or getattr(fn, "__name__", "interceptor")
        hook = _Hook(fn, HookInfo(phase, order, owner, label, on_error), self._seq)
        self._hooks.append(hook)
        self._resort()

        def dispose() -> None:
            if hook in self._hooks:
                self._hooks.remove(hook)
                self._resort()

        return dispose

    def _resort(self) -> None:
        self._sorted = tuple(
            sorted(
                self._hooks,
                key=lambda h: (self._rank[h.info.phase], h.info.order, h.info.owner, h.info.name, h.seq),
            )
        )

    def describe(self) -> list[HookInfo]:
        """当前生效的链（自省接口用）。"""
        return [hook.info for hook in self._sorted]

    def stats(self) -> list[dict[str, object]]:
        """逐拦截器的调用 / 出错 / 自身耗时累计（挂 ``/healthz`` 指标）。"""
        return [
            {
                "phase": hook.info.phase,
                "owner": hook.info.owner,
                "name": hook.info.name,
                "calls": hook.calls,
                "errors": hook.errors,
                "self_ms": round(hook.self_seconds * 1000, 3),
            }
            for hook in self._sorted
        ]

    async def run(self, ctx: C, terminal: Terminal[C, R]) -> R:
        """依序穿过拦截链，最内层调用 ``terminal``（真正的处理函数）。"""
        hooks = self._sorted

        async def call(index: int) -> R:
            if index >= len(hooks):
                return await terminal(ctx)
            hook = hooks[index]
            frame = _Frame[R]()

            async def nxt() -> R:
                if frame.called:
                    raise NextCalledTwice("next() called multiple times")
                frame.called = True
                started = time.perf_counter()
                try:
                    result = await call(index + 1)
                except BaseException as exc:
                    frame.downstream_error = exc
                    raise
                finally:
                    frame.downstream_seconds += time.perf_counter() - started
                frame.result, frame.has_result = result, True
                return result

            started = time.perf_counter()
            hook.calls += 1
            try:
                return await hook.fn(ctx, nxt)
            except asyncio.CancelledError:
                raise
            except NextCalledTwice:
                raise  # 编码错误：fail-open 也不吞（§4.3）
            except Exception as exc:
                if exc is frame.downstream_error:
                    raise  # 下游自己的错照常传播，不计本拦截器（§4.3）
                hook.errors += 1
                if hook.info.on_error is OnError.FAIL:
                    raise
                self._report(hook, exc)
                if frame.has_result:
                    return cast(R, frame.result)
                if frame.called:
                    raise
                skipped_at = time.perf_counter()
                try:
                    return await call(index + 1)
                finally:
                    frame.downstream_seconds += time.perf_counter() - skipped_at
            finally:
                hook.self_seconds += time.perf_counter() - started - frame.downstream_seconds

        return await call(0)

    def _report(self, hook: _Hook[C, R], exc: Exception) -> None:
        if self._on_problem is not None:
            info = hook.info
            self._on_problem(f"拦截器 {info.owner}/{info.name}（{info.phase}）出错已跳过：{type(exc).__name__}: {exc}")


__all__: list[str] = [
    "HookChain",
    "HookInfo",
    "Interceptor",
    "Next",
    "NextCalledTwice",
    "OnError",
    "Terminal",
]
