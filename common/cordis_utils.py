import asyncio
import inspect
import time
from collections.abc import Callable

from cordis_port import Context, Fiber


async def safe_dispose(fiber: Fiber) -> BaseException | None:
    """销毁 fiber 并吞掉清理期异常（含 events.emit 监听器异常）。

    返回捕获到的异常供调用方记日志 / 决策；``dispose()`` 对已清理的 fiber
    可能返回 ``None``，这里统一判空，不交给调用方各自处理。

    用 ``gather(return_exceptions=True)`` 收集异常而不是盲 ``except``：清理期
    任何异常（含 ``dispose()`` 同步抛出）都折算成返回值；外层取消不在此列——
    ``CancelledError`` 照常向上传播，不被误当作清理失败吞掉。
    """

    async def _dispose() -> None:
        result = fiber.dispose()
        if inspect.isawaitable(result):
            await result

    outcome = await asyncio.gather(_dispose(), return_exceptions=True)
    error = outcome[0]
    return error if isinstance(error, BaseException) else None


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

    单个 fiber 清理失败**不再中断整个卸载循环**——全部跑完后统一记日志，
    保证 web 服务等关键资源不会因某个插件的清理异常而来不及停止。
    """
    unloaded: list[Fiber] = []
    failures: list[BaseException] = []
    # 复制快照：dispose 过程中会修改 registry._internal，不能直接迭代原集合。
    # **逆序**卸载（后注册先销毁）：依赖方先于被依赖方收尾——db 排空连接池必须
    # 晚于 log / comm 等使用方把后台任务收干净，否则任务在关停后归还连接，
    # Windows 上文件句柄不释放
    for runtime in reversed(list(ctx.registry.values())):
        for fiber in reversed(list(runtime.fibers)):
            if fiber.uid is None:
                # 已经卸载过（uid 置空是销毁的标志）
                continue
            error = await safe_dispose(fiber)
            if error is not None:
                failures.append(error)
            unloaded.append(fiber)
    for error in failures:
        ctx.logger.error("卸载期间 fiber 清理失败：%s", error)
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

