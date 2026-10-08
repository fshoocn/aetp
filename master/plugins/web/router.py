"""路由注册表：登记路由、包装 handler、维护 Starlette 路由表。

本模块是 web 插件的核心，但不直接对外暴露 —— 外部一律通过
:class:`~plugins.web.interface.WebService` 的便捷方法使用。

几条关键设计：

1. **两种注册形态**：装饰器与显式调用共用同一实现。
   ``ctx.web.get("/x")`` 返回装饰器；``ctx.web.get("/x", handler)`` 直接登记。
   两者都返回原 handler，所以 ``@ctx.web.get(...)`` 不会改变被装饰函数的身份。

2. **路由随注册方插件卸载**：注册时记录调用方 fiber（由 cordis 的 shadow 机制
   提供），并把整条记录登记到该 fiber 的 effect 上。插件卸载或调用
   ``ctx.web.remove(path)`` 时，路由自动从 Starlette 路由表移除。

3. **路径参数自动注入**：``/items/{item_id}`` 里的 ``item_id`` 会按名匹配 handler
   的形参并作为关键字传入（前提是形参带注解）；未声明注解的形参不注入，
   handler 可从 ``request.path_params`` 自行读取。

4. **同步/异步 handler 通吃**：包装层统一 ``await``，因此插件既可以写普通函数，
   也可以写 ``async def``。

5. **路径参数类型转换**：形参注解为 ``int`` / ``float`` / ``bool`` / ``Path`` 时
   自动转换（Starlette 只会给字符串）；转换失败返回 400 而不是 500。

6. **同一路径多方法共存**：路由表按「路径 + 方法」管理，``GET /x`` 与
   ``POST /x`` 可以各自绑定不同的 handler；方法重叠时后注册者覆盖。
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable, Iterator, Sequence
from pathlib import Path
from string import Formatter
from typing import Protocol, cast, get_type_hints

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute

from .types import HTTP_METHODS, HttpMethod, RouteHandler, RouteKind, RouteRecord

#: 支持的 HTTP 方法（大写，便于与 Starlette 对接）
METHODS: tuple[HttpMethod, ...] = HTTP_METHODS


class RouteError(ValueError):
    """路由注册失败（路径非法、重复注册、handler 不可调用等）。"""


class _EffectOwner(Protocol):
    def effect(
        self,
        execute: Callable[[], Callable[[], bool] | None],
        label: str = "anonymous",
    ) -> Callable[[], object]: ...


def parse_path_params(path: str) -> list[str]:
    """从路径模板中提取参数名，例如 ``/items/{item_id}`` → ``["item_id"]``。

    使用 :class:`string.Formatter` 解析，兼容 ``{name}`` 与 ``{name:path}`` 两种
    Starlette 写法。
    """
    names: list[str] = []
    for _, field_name, _, _ in Formatter().parse(path):
        if not field_name:
            continue
        # 去掉类型后缀：Starlette 的 ``{p:path}`` 在这里表现为 field_name="p:path"
        name = field_name.split(":")[0].split(".")[0].strip()
        if name and name not in names:
            names.append(name)
    return names


def normalize_path(path: object, *, allow_root: bool = True) -> str:
    """把外部传入的路径规整为站内绝对路径（``foo/`` → ``/foo``）。"""
    if not isinstance(path, str):
        raise RouteError(f"路由路径必须是 str，收到 {type(path).__name__}")
    value = path.strip()
    if not value:
        if allow_root:
            return "/"
        raise RouteError("路由路径不能为空")
    if not value.startswith("/"):
        value = "/" + value
    # 折叠重复斜杠，但保留根路径
    while "//" in value:
        value = value.replace("//", "/")
    if len(value) > 1:
        value = value.rstrip("/") or "/"
    return value


def normalize_methods(methods: str | Sequence[str] | None) -> frozenset[HttpMethod]:
    """归一化 HTTP 方法集合（缺省为 GET，统一大写并去重）。"""
    if methods is None:
        return frozenset({"GET"})
    if isinstance(methods, str):
        items = methods.replace(",", " ").split()
    else:
        items = list(methods)
    resolved = {str(item).strip().upper() for item in items if str(item).strip()}
    if not resolved:
        raise RouteError("methods 不能为空")
    unknown = resolved - set(METHODS)
    if unknown:
        raise RouteError(f"不支持的 HTTP 方法：{', '.join(sorted(unknown))}")
    return frozenset(cast(HttpMethod, method) for method in resolved)


def injectable_params(handler: RouteHandler, names: Sequence[str]) -> frozenset[str]:
    """计算需要按路径参数注入的形参名。

    只有「handler 显式声明了该形参、且该形参带类型注解」时才注入，避免误把
    ``request`` 之类的常规形参当成路径参数。若 handler 声明了 ``**kwargs``，
    则全部路径参数都注入。

    注解必须经 :func:`_resolved_annotations` 还原：插件普遍使用
    ``from __future__ import annotations``，此时 ``inspect`` 拿到的是字符串。
    """
    if not names:
        return frozenset()
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):  # 内建函数等无法内省
        return frozenset()

    accepts_kwargs = any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()
    )
    if accepts_kwargs:
        return frozenset(names)

    hints = _resolved_annotations(handler)
    injectable: set[str] = set()
    for name in names:
        param = signature.parameters.get(name)
        if param is None:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.POSITIONAL_ONLY):
            continue
        # 有注解才认为 handler 是有意接收该路径参数（未注解时可能是任意同名形参）
        if param.annotation is inspect.Parameter.empty and name not in hints:
            continue
        injectable.add(name)
    return frozenset(injectable)


def _resolved_annotations(handler: RouteHandler) -> dict[str, object]:
    """解析 handler 的形参注解，把字符串注解还原为真实类型。

    插件普遍使用 ``from __future__ import annotations``，此时
    ``inspect.signature`` 给出的注解是字符串（``"int"`` 而非 ``int``），无法直接
    用于类型判断。``typing.get_type_hints`` 会按其定义模块的命名空间求值，
    把字符串还原成真实类型。

    求值失败（例如注解引用了未导入的名字）时返回空表：此时退化为「不注入、
    也不转换」，由 handler 从 ``request.path_params`` 自行读取。
    """
    try:
        return get_type_hints(handler)
    except Exception:  # noqa: BLE001 - 注解求值失败不应影响路由注册
        return {}


def _parse_bool(value: str) -> bool:
    """把常见布尔文本解析为 bool，拒绝含糊输入。"""
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"无法将 {value!r} 转换为 bool")


def _converter(annotation: object) -> Callable[[str], int | float | bool | Path] | None:
    """由形参注解推导路径参数转换函数；无需转换时返回 ``None``。"""
    if annotation is inspect.Parameter.empty or annotation is str:
        return None
    # 兼容 ``Annotated[int, ...]`` 写法
    origin = getattr(annotation, "__origin__", None)
    if hasattr(annotation, "__metadata__") and origin is not None:
        annotation = origin
    if annotation is int:
        return int
    if annotation is float:
        return float
    if annotation is bool:
        return _parse_bool
    if annotation is Path:
        return Path
    return None


def param_converters(
    handler: RouteHandler, names: Sequence[str]
) -> dict[str, Callable[[str], int | float | bool | Path]]:
    """为需要注入的路径参数计算「字符串 → 注解类型」的转换函数。

    Starlette 交给 handler 的路径参数永远是字符串（``/items/42`` 得到 ``"42"``），
    这里按注解自动转换，避免每个插件都手写一遍 ``int(item_id)``。
    无法推导转换函数时不放入表中，端点将原样传字符串。
    """
    converters: dict[str, Callable[[str], int | float | bool | Path]] = {}
    if not names:
        return converters
    hints = _resolved_annotations(handler)
    if not hints:
        return converters
    for name in names:
        converter = _converter(hints.get(name, inspect.Parameter.empty))
        if converter is not None:
            converters[name] = converter
    return converters


def build_endpoint(record: RouteRecord) -> Callable[[Request], Awaitable[Response]]:
    """把用户 handler 包装成 Starlette 可用的异步端点。

    包装层的职责：

    * 注入路径参数（见 :func:`injectable_params`）；
    * 兼容同步/异步 handler；
    * 把返回值交给 :func:`~plugins.web.responses.to_response` 转换；
    * 用 ``functools.wraps`` 保留原函数的 ``__name__``/``__doc__``，便于排查。
    """
    from .responses import to_response

    handler = record.handler
    assert handler is not None  # 由调用方保证
    params = {name: name for name in record.params}
    converters: dict[str, Callable[[str], object]] = dict(record.converters or {})

    async def endpoint(request: Request) -> Response:
        kwargs: dict[str, object] = {}
        for name, arg in params.items():
            raw = request.path_params[name]
            converter = converters.get(name)
            if converter is None:
                kwargs[arg] = raw
                continue
            try:
                kwargs[arg] = converter(raw)
            except (TypeError, ValueError) as exc:
                expected_type = "bool" if converter is _parse_bool else converter.__name__
                raise HTTPException(
                    status_code=400,
                    detail=f"路径参数 {name!r} 需要 {expected_type} 类型，收到 {raw!r}",
                ) from exc
        result: object | Awaitable[object] = handler(request, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return to_response(result, record)

    functools.wraps(handler)(endpoint)
    return endpoint


class RouteRegistry:
    """路由表：管理 :class:`RouteRecord`，并按需同步到 Starlette 应用。

    该对象由 :class:`~plugins.web.uvicorn_web_service.UvicornWebService` 持有，生命周期与
    web 插件 fiber 一致。``app`` 为 ``None`` 时只做登记（服务器尚未启动），
    绑定后再统一挂载，因此插件加载顺序不受限制。
    """

    def __init__(self) -> None:
        #: 路径 -> 该路径下的路由记录（**列表**：同一路径可挂不同 HTTP 方法）
        self.routes: dict[str, list[RouteRecord]] = {}
        #: 静态目录记录（静态挂载不参与方法冲突判断）
        self.statics: dict[str, RouteRecord] = {}
        #: 当前绑定的 Starlette 应用；``None`` 表示服务器尚未启动（只登记不挂载）
        self._app: Starlette | None = None

    def __iter__(self) -> Iterator[RouteRecord]:
        """按注册顺序遍历全部非静态路由记录。"""
        for records in self.routes.values():
            yield from records

    def __len__(self) -> int:
        return sum(len(records) for records in self.routes.values())

    # -- 与 Starlette 对接 ----------------------------------------------------
    @property
    def app(self) -> Starlette | None:
        """当前绑定的 Starlette 应用（未绑定时为 ``None``）。"""
        return self._app

    def attach_app(self, app: Starlette) -> None:
        """绑定 Starlette 应用，并把已登记的路由全部挂上。"""
        self._app = app
        for record in self:
            self._validate(record)
            self._mount(record)
        for record in self.statics.values():
            self._mount_static(record)

    def detach_app(self) -> None:
        """解绑应用并清空已挂载的 Starlette 路由对象。"""
        self._app = None
        for record in [*self, *self.statics.values()]:
            record.route = None

    def _validate(self, record: RouteRecord) -> None:
        """确保端点已构造（handler 可能在应用就绪后才补上）。"""
        if record.endpoint is None and record.handler is not None:
            record.endpoint = build_endpoint(record)

    def _mount(self, record: RouteRecord) -> None:
        """把一条记录挂到 Starlette 路由表（幂等）。"""
        if self._app is None or record.route is not None or record.endpoint is None:
            return
        from starlette.routing import Route

        record.route = Route(
            record.path,
            record.endpoint,
            methods=sorted(record.methods),
            name=record.name or record.path,
        )
        self._app.router.routes.append(record.route)

    def _mount_static(self, record: RouteRecord) -> None:
        """挂载静态目录（``Mount``，不参与 HTTP 方法匹配）。"""
        if self._app is None or record.route is not None:
            return
        from starlette.routing import Mount
        from starlette.staticfiles import StaticFiles

        directory = Path(record.directory or "")
        if not directory.is_dir():
            raise RouteError(f"静态目录不存在：{directory}")
        record.route = Mount(
            record.path,
            app=StaticFiles(directory=str(directory), html=record.html),
            name=record.name or record.path,
        )
        self._app.router.routes.append(record.route)

    def _unmount(self, record: RouteRecord) -> None:
        """从 Starlette 路由表移除一条记录。"""
        if self._app is not None and record.route is not None:
            try:
                self._app.router.routes.remove(cast(BaseRoute, record.route))
            except ValueError:
                pass
        record.route = None

    # -- 注册 / 注销 ----------------------------------------------------------
    def add(
        self,
        record: RouteRecord,
        owner_fiber: _EffectOwner | None = None,
    ) -> None:
        """登记路由并按需挂载。

        冲突规则（只比较 HTTP 方法是否重叠）：

        * 路径不同 -> 各自独立；
        * 路径相同、方法**不重叠** -> 共存（``GET /x`` 与 ``POST /x`` 可分别为
          不同 handler）；
        * 路径相同、方法**重叠** -> 后注册者覆盖，旧记录先摘除。

        参数:
            record: 待登记的路由记录。
            owner_fiber: 负责该路由的 fiber；给出时会把清理函数注册成它的
                effect，使路由随插件卸载而自动移除。
        """
        if record.is_static:
            # 静态挂载按前缀唯一，重复挂载时后者覆盖
            replaced_static = self.statics.get(record.path)
            if replaced_static is not None:
                self._unmount(replaced_static)
            self.statics[record.path] = record
            self._mount_static(record)
        else:
            records = self.routes.setdefault(record.path, [])
            for old in list(records):
                if old.methods & record.methods:
                    # 方法重叠：后注册者覆盖
                    self._unmount(old)
                    records.remove(old)
            self._validate(record)
            records.append(record)
            self._mount(record)

        if owner_fiber is not None:
            # 用 effect 绑定记录身份：同路径的其他方法或后续替代路由不受影响。
            owner_fiber.effect(
                lambda: lambda: self._remove_record(record),
                f"ctx.web.route({record.path!r})",
            )

    def remove(self, path: str) -> bool:
        """注销该路径下的**全部**路由（含静态目录），返回是否确实删掉了内容。"""
        removed = False
        for record in self.routes.pop(path, []):
            self._unmount(record)
            removed = True
        static = self.statics.pop(path, None)
        if static is not None:
            self._unmount(static)
            removed = True
        return removed

    def _remove_record(self, record: RouteRecord) -> bool:
        """只注销指定记录，避免旧 owner 的清理回调误删后来注册的同路径路由。"""
        if record.is_static:
            if self.statics.get(record.path) is not record:
                return False
            self.statics.pop(record.path)
        else:
            records = self.routes.get(record.path)
            if records is None:
                return False
            remaining = [item for item in records if item is not record]
            if len(remaining) == len(records):
                return False
            if remaining:
                self.routes[record.path] = remaining
            else:
                self.routes.pop(record.path, None)
        self._unmount(record)
        return True

    def clear(self) -> None:
        """清空全部路由（web 插件卸载时调用）。"""
        for record in [*self, *self.statics.values()]:
            self._unmount(record)
        self.routes.clear()
        self.statics.clear()
        self.detach_app()

    # -- 查询 ----------------------------------------------------------------
    def get(self, path: str) -> RouteRecord | None:
        """按路径取**第一条**路由记录（同路径多方法时只返回首个）。"""
        records = self.routes.get(path)
        if records:
            return records[0]
        return self.statics.get(path)

    def get_all(self, path: str) -> list[RouteRecord]:
        """按路径取全部路由记录（同路径可挂多个 HTTP 方法）。"""
        records = list(self.routes.get(path, []))
        static = self.statics.get(path)
        if static is not None:
            records.append(static)
        return records

    def list_paths(self) -> list[str]:
        """按注册顺序返回全部路由路径（含静态目录，已去重）。"""
        return [*self.routes, *self.statics]


__all__: list[str] = [
    "METHODS",
    "RouteError",
    "RouteKind",
    "RouteRegistry",
    "build_endpoint",
    "injectable_params",
    "normalize_methods",
    "normalize_path",
    "param_converters",
    "parse_path_params",
]
