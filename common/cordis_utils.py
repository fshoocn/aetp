import asyncio
import time
from collections.abc import Callable

from cordis_port import Context, Fiber

_ctx: "Context | None" = None

def set_ctx(ctx: "Context") -> None:
    global _ctx
    if _ctx is not None and _ctx is not ctx:
        raise RuntimeError("ctx 已经初始化过了，重复设置说明有多个启动路径")
    _ctx = ctx

def get_ctx() -> "Context":
    if _ctx is None:
        raise RuntimeError("ctx 尚未初始化，请先在入口调用 set_ctx()")
    return _ctx


async def unload_all(ctx: Context) -> list[Fiber]:
    """卸载上下文树中的**全部插件**，返回被卸载的 fiber 列表。

    为什么需要这个函数：``ctx.fiber.dispose()`` 对**根 fiber** 并不卸载插件，
    它的实现是「重启」（见 cordis ``Fiber._root_dispose`` —— 清空后重建根级
    effect），因此进程退出时用它无法释放插件占用的资源（例如 web 服务监听的端口）。

    正确做法是遍历 ``ctx.registry`` 里登记的所有插件 fiber 逐个 ``dispose()``：
    每个 fiber 的销毁会执行其 effect 清理链，且 ``take(1)`` 语义（后注册先销毁）
    会保证依赖方先于被依赖方卸载 —— 例如业务插件先卸载，web 服务最后停止。

    参数:
        ctx: 任意上下文；内部会取其 ``registry``（四类核心服务在上下文树中共享，
            因此传子上下文同样有效）。

    返回:
        已卸载的 fiber 列表（按卸载顺序）。
    """
    unloaded: list[Fiber] = []
    # 复制快照：dispose 过程中会修改 registry._internal，不能直接迭代原集合
    for runtime in list(ctx.registry.values()):
        for fiber in list(runtime.fibers):
            if fiber.uid is None:
                # 已经卸载过（uid 置空是销毁的标志）
                continue
            await fiber.dispose()
            unloaded.append(fiber)
    return unloaded


async def wait_until(
    predicate: Callable[[], object],
    timeout: float = 10.0,
    interval: float = 0.05,
) -> bool:
    """轮询等待 ``predicate()`` 返回真值；超时返回 ``False``。

    常用于「等待某个服务就绪」这类跨进程/线程的时序协调。

    参数:
        predicate: 无参可调用对象，返回真值表示条件满足。
        timeout: 最长等待秒数。
        interval: 轮询间隔秒数。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())

