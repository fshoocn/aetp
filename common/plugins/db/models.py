"""模型基类与物理表名改写（per-owner ``MetaData``、归属前缀、重装幂等）。

插件声明模型只给**逻辑表名**（类名蛇形或显式 ``__tablename__``）::

    from common.plugins.db import Model, Mapped, mapped_column

    class TestRun(Model):
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]

物理表名由本模块在注册时改写为 ``aetp_{owner}_{logical}``（owner = 插件 id），
归属因此天然进表名；索引名同样加 owner 前缀，超长按
:attr:`MAX_IDENT` 截断 + sha256 后缀防撞。

**每个 owner 独立 ``MetaData`` / registry**（DESIGN §11-3）：插件重装 /
热更新会重执行模块，同名模型重复注册不会炸 —— 检测到同类重定义时该 owner
的注册组整体换新，旧映射作废；不同类抢同一逻辑表名则直接报错。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, ClassVar, Final, cast

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    Time,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm import registry as sa_registry
from sqlalchemy.sql.elements import quoted_name
from sqlalchemy.sql.type_api import TypeEngine

from .registry import TYPE_TOKENS

#: 物理表名前缀（全部归属共享）
TABLE_PREFIX: Final[str] = "aetp_"
#: 标识符长度兜底（Oracle 老版本 30）
MAX_IDENT: Final[int] = 30
#: 插件管理器合成包名前缀（``_aetp_plugin_<插件 id 的 ascii hex>``）
SYNTHETIC_PREFIX: Final[str] = "_aetp_plugin_"


def snake(name: str) -> str:
    """类名 → 蛇形逻辑表名（``TestRun`` → ``test_run``）。"""
    stepped = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", stepped).lower()


def slug(name: str) -> str:
    """归属 / 逻辑名 → 物理名安全段（小写 ``[a-z0-9_]``，其余折叠为 ``_``）。"""
    return re.sub(r"[^a-z0-9_]", "_", name.lower())


def compact(name: str) -> str:
    """标识符超长时保留前段 + 8 位 sha256 后缀防撞（:attr:`MAX_IDENT` 兜底）。"""
    if len(name) <= MAX_IDENT:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    keep = MAX_IDENT - 9
    return f"{name[:keep]}_{digest}"


def physical_name(owner: str, logical: str) -> str:
    """物理表名：``{prefix}{owner}_{logical}``（全小写、超长截断防撞）。"""
    return compact(f"{TABLE_PREFIX}{slug(owner)}_{slug(logical)}")


def owner_index_name(owner: str, name: str) -> str:
    """索引名同样加 owner 前缀（DESIGN §11 实现补充）。"""
    return compact(f"{TABLE_PREFIX}{slug(owner)}_{slug(name)}")


def type_token(type_: TypeEngine[object] | object) -> str:
    """把 SQLAlchemy 列类型归一为 ``requires`` 声明用的类型符（见 ``TYPE_TOKENS``）。"""
    if isinstance(type_, Boolean):
        return "bool"
    if isinstance(type_, (Integer, SmallInteger, BigInteger)):
        return "int"
    if isinstance(type_, Float):
        return "float"
    if isinstance(type_, Numeric):
        return "decimal"
    if isinstance(type_, DateTime):
        return "datetime"
    if isinstance(type_, Date):
        return "date"
    if isinstance(type_, Time):
        return "time"
    if isinstance(type_, JSON):
        return "json"
    if isinstance(type_, LargeBinary):
        return "bytes"
    if isinstance(type_, Uuid):
        return "uuid"
    if isinstance(type_, (String, Text)):
        return "str"
    return type(type_).__name__.lower()


def decode_plugin_id(module: str) -> str | None:
    """合成包名 ``_aetp_plugin_<hex>`` → 插件 id（插件管理器装载插件的约定）。"""
    head = module.split(".", 1)[0]
    if not head.startswith(SYNTHETIC_PREFIX):
        return None
    try:
        return bytes.fromhex(head[len(SYNTHETIC_PREFIX) :]).decode("ascii")
    except ValueError:
        return None


def module_owner(module: str) -> str:
    """模型定义处的默认归属：合成包 → 插件 id；否则取顶层模块名。"""
    return decode_plugin_id(module) or module.split(".", 1)[0]


#: 裸 ``dict`` 注解的归一化写法（类型映射键用；运行时值就是 ``dict`` 本体）
_DICT: Final[type[object]] = cast(type[object], dict)
#: 注解 → JSON 列的全部键形态（裸 dict / 泛型别名，含 Optional）
_JSON_KEYS: Final[tuple[object, ...]] = (
    _DICT,
    _DICT | None,
    dict[str, Any],
    dict[str, Any] | None,
)


class _OwnerGroup:
    """一个 owner 的模型注册组：独立 registry + ``MetaData``。

    隔离作用域：``drop_owner`` 只动本组的表，重装 / 热更新重执行模块时
    整组换新（旧映射作废），同名模型注册天然幂等。
    """

    def __init__(self, owner: str) -> None:
        self.owner: str = owner
        self.metadata: MetaData = MetaData()
        self.registry = sa_registry(
            metadata=self.metadata, type_annotation_map={key: JSON for key in _JSON_KEYS}
        )
        #: (模块, 限定名) → 模型类；同键重出现 = 模块重执行（重装 / 热更新）
        self.classes: dict[tuple[str, str], type[Model]] = {}
        #: 逻辑表名 → 类键（不同类抢同一逻辑表名即冲突）
        self.logical_owners: dict[str, tuple[str, str]] = {}
        self.models: list[type[Model]] = []


_GROUPS: dict[str, _OwnerGroup] = {}
#: 正在定义的模型类（``__table_cls__`` 取归属用；类定义同步完成，无并发）
_CURRENT: list[type[Model]] = []


def _new_group(owner: str) -> _OwnerGroup:
    group = _OwnerGroup(owner)
    _GROUPS[owner] = group
    return group


class Model(DeclarativeBase):
    """轻量声明式模型基类：提供逻辑表名，物理表名由 db 改写并挂到 owner 归属下。

    * ``__owner__``：显式归属（可选）——不写则按定义模块推断
      （合成包 → 插件 id，否则顶层模块名）；
    * ``__tablename__``：显式逻辑表名（可选）——不写取类名蛇形；
    * ``Mapped`` / ``mapped_column`` 直接用 SQLAlchemy 原语（本层只管命名与
      归属，不发明新语法）。
    """

    #: 注解 → 列类型映射（JSON 等通用类型由本层统一兑底，方言差异见 DESIGN §5）
    type_annotation_map: ClassVar[dict[object, object]] = {key: JSON for key in _JSON_KEYS}

    #: 显式归属（可选）；不写按定义模块推断
    __owner__: ClassVar[str | None] = None
    #: 解析出的归属（注册时写入）
    __model_owner__: ClassVar[str] = ""
    #: 解析出的逻辑表名（注册时写入）
    __model_logical__: ClassVar[str] = ""

    def __init_subclass__(cls, **kw: object) -> None:
        inherited = next(
            (base for base in cls.__bases__ if getattr(base, "__table__", None) is not None),
            None,
        )
        if inherited is not None:
            # 单表继承：共享父表，不另行认领逻辑表名
            cls.__model_owner__ = getattr(inherited, "__model_owner__", "") or module_owner(cls.__module__)
            cls.__model_logical__ = getattr(inherited, "__model_logical__", "") or snake(cls.__name__)
            super().__init_subclass__(**kw)
            return

        owner = getattr(cls, "__owner__", None) or module_owner(cls.__module__)
        logical = cls.__dict__.get("__tablename__")
        if not isinstance(logical, str) or not logical:
            logical = snake(cls.__name__)
        is_abstract = bool(cls.__dict__.get("__abstract__", False))
        key = (cls.__module__, cls.__qualname__)
        group = _GROUPS.get(owner)
        if group is not None and key in group.classes:
            # 同一类重定义（插件重装 / 热更新重执行模块）→ 整组换新，旧映射作废
            group = _new_group(owner)
        if group is None:
            group = _new_group(owner)
        elif (
            not is_abstract
            and logical in group.logical_owners
            and group.logical_owners[logical] != key
        ):
            other = group.logical_owners[logical]
            raise ValueError(
                f"逻辑表名 {logical!r}（owner={owner!r}）已被 {other[1]!r} 认领，"
                f"请改类名或显式 __tablename__ 区分"
            )
        group.classes[key] = cls
        if not is_abstract:
            group.logical_owners[logical] = key
            group.models.append(cls)
        cls.__model_owner__ = owner
        cls.__model_logical__ = logical
        # 每个 owner 独立 registry：同名模型重注册不冲突（DESIGN §11-3）
        cls._sa_registry = group.registry
        if not is_abstract and not isinstance(cls.__dict__.get("__tablename__"), str):
            # 默认逻辑表名 = 类名蛇形（declarative 不做自动命名，这里补上）
            cls.__tablename__ = logical
        _CURRENT.append(cls)
        try:
            super().__init_subclass__(**kw)
        except BaseException:
            # 定义期失败（如跨 owner 外键拒绝）回滚登记（DB-N10），
            # 避免幽灵占用让同进程后续同名定义被误判冲突
            group.classes.pop(key, None)
            if not is_abstract:
                if group.logical_owners.get(logical) == key:
                    del group.logical_owners[logical]
                if cls in group.models:
                    group.models.remove(cls)
            raise
        finally:
            _CURRENT.pop()

    @staticmethod
    def __table_cls__(name: str, metadata: MetaData, *args: Any, **kw: Any) -> Table[Any]:
        """表创建拦截：改写物理名（归属前缀）、路由到 owner ``MetaData``。

        接收 SQLAlchemy 传入的 ``(名字, metadata, *列, **参数)``
        （静态函数，SQLAlchemy 经 ``cls.__table_cls__`` 取用后直接调用）。
        """
        if not _CURRENT:
            raise RuntimeError("模型表创建缺少定义上下文（Model 只能经类声明使用）")
        cls = _CURRENT[-1]
        owner = cls.__model_owner__
        group = _GROUPS[owner]
        physical = physical_name(owner, name)
        cls.__model_logical__ = name
        existing = group.metadata.tables.get(physical)
        if existing is not None:
            # 兜底：可变 __tablename__（declared_attr）导致预检漏判时，仍按重注册替换
            group.metadata.remove(existing)
        table = Table(physical, group.metadata, *args, **kw)
        for foreign_key in table.foreign_keys:
            target = foreign_key.column.table
            if target.metadata is not group.metadata:
                raise ValueError(
                    f"禁止跨 owner 外键：{physical} → {target.name}；"
                    f"联查用 external_model / 查询 join，不绑结构（DESIGN §11-2）"
                )
        for index in table.indexes:
            current = index.name or ""
            if physical not in current:
                # 索引名加 owner 前缀防跨归属撞名；name 声明为 quoted_name，按声明类型赋值
                index.name = quoted_name(owner_index_name(owner, current or f"ix_{name}"), None)
            elif len(current) > MAX_IDENT:
                index.name = quoted_name(compact(current), None)
        return table

    def __init__(self, **values: object) -> None:
        """声明式构造：关键字参数赋到同名属性（``TestRun(name="x")``）。"""
        for name, value in values.items():
            if not hasattr(type(self), name):
                raise TypeError(f"{type(self).__name__}() 不认识参数 {name!r}")
            setattr(self, name, value)


def model_owner(model: type[Model]) -> str:
    """模型的归属 owner（注册时解析：显式 ``__owner__`` → 定义模块推断）。"""
    declared = getattr(model, "__model_owner__", "") or ""
    return declared or module_owner(model.__module__)


def model_logical(model: type[Model]) -> str:
    """模型的逻辑表名（显式 ``__tablename__`` 或类名蛇形）。"""
    declared = getattr(model, "__model_logical__", "") or ""
    return declared or snake(model.__name__)


def owner_models(owner: str) -> list[type[Model]]:
    """某 owner 名下已注册的全部模型（按注册顺序）。"""
    group = _GROUPS.get(owner)
    return list(group.models) if group is not None else []


def registered_models() -> list[type[Model]]:
    """全部已注册模型（跨 owner；无归属信息的调用方兜底用）。"""
    models: list[type[Model]] = []
    for group in _GROUPS.values():
        models.extend(group.models)
    return models


def reset_owner_group(owner: str) -> None:
    """作废某 owner 的注册组（重装隔离 / 测试夹具用；已映射类随之失效）。"""
    _GROUPS.pop(owner, None)


__all__: list[str] = [
    "MAX_IDENT",
    "TYPE_TOKENS",
    "Mapped",
    "Model",
    "decode_plugin_id",
    "mapped_column",
    "model_logical",
    "model_owner",
    "module_owner",
    "owner_index_name",
    "owner_models",
    "physical_name",
    "registered_models",
    "reset_owner_group",
    "snake",
    "type_token",
]
