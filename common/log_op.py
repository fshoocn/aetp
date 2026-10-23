"""操作关联 id（轻量 trace）：一次管理操作的日志一键聚齐。

用法::

    with log_operation():          # 形如 ``op-1a2b3c``
        await manager.enable(...)  # 作用域内所有日志自动带 op 标记

日志出口（入库 / 上报）自动读取当前操作 id 落到 ``op`` 列；查看页
「查看本次操作」= ``q=op-1a2b3c``。将来用户管理插件上线可在作用域上
叠加「操作者」元数据。
"""

from __future__ import annotations

import functools
import secrets
from collections.abc import Callable, Coroutine
from contextvars import ContextVar, Token
from typing import Any, ParamSpec, TypeVar

_OPERATION_ID: ContextVar[str | None] = ContextVar("aetp_log_operation", default=None)

_P = ParamSpec("_P")
_R = TypeVar("_R")


class _LogOperation:
    """操作作用域：``with _LogOperation() as op``（作用域内日志自动带 op 标记）。"""

    def __init__(self) -> None:
        self.operation: str = f"op-{secrets.token_hex(3)}"
        self._token: Token[str | None] | None = None

    def __enter__(self) -> str:
        self._token = _OPERATION_ID.set(self.operation)
        return self.operation

    def __exit__(self, *_exc: object) -> None:
        if self._token is not None:
            _OPERATION_ID.reset(self._token)


def log_operation() -> _LogOperation:
    """管理操作作用域：作用域内所有日志自动带 ``op-xxxxxx`` 标记。"""
    return _LogOperation()


def current_operation() -> str | None:
    """当前操作 id（无操作作用域时 ``None``）。"""
    return _OPERATION_ID.get()


def log_operation_async(
    func: Callable[_P, Coroutine[Any, Any, _R]],
) -> Callable[_P, Coroutine[Any, Any, _R]]:
    """装饰器形态：整次异步调用纳入操作作用域（管理面入口用）。"""

    @functools.wraps(func)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with log_operation():
            return await func(*args, **kwargs)

    return wrapper


__all__: list[str] = ["current_operation", "log_operation", "log_operation_async"]
