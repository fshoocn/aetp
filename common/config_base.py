"""插件配置模型基类：字段表驱动的默认值、Schema 校验与 ini 双向转换。

把各插件配置类里重复的样板（默认值填充、``validate`` 类型循环、``merge``
层合并、``from_ini``/``to_ini`` 序列化）收敛到一处，配置类只声明**字段表**
与少量专属校验：

* :attr:`FieldConfig._FIELDS` —— ``字段名 -> (类型元组, 说明)``；
* 类属性 —— 各字段默认值（与字段同名）；
* :attr:`FieldConfig.ini_section` —— 对应的 ``config.ini`` 节名（可选）。

``ini_section`` 是 appconfig 两个生命周期钩子的接线约定：

* ``internal/config`` —— 每次解析插件配置（加载 / ``fiber.update()``）时，
  把该节的 ini 值注入为**默认值层**：显式传入的配置覆盖 ini 值，ini 值覆盖
  类属性默认值；改写发生在 Schema 校验之前（见
  :meth:`common.plugins.appconfig.plugin.AppConfigPlugin`）。
* ``internal/commit`` —— ``fiber.update(...)`` 提交时把新配置经
  :meth:`FieldConfig.to_ini` 序列化写回该节（``no_save=True`` 跳过）。

这样「配置从哪来、写回哪去」全部由配置类声明一次，插件与
``config.ini`` 的手工接线（``from_ini(ctx.appconfig.section(...))``）就省掉了。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Self, cast

#: 布尔值的合法写法（与 :class:`common.plugins.appconfig.config.AppConfig` 的
#: 解析口径一致）
_TRUE_WORDS: frozenset[str] = frozenset({"1", "yes", "true", "on"})
_FALSE_WORDS: frozenset[str] = frozenset({"0", "no", "false", "off"})

#: 标准 Schema 的单条校验问题（与 cordis 的 ``ValidationError`` 载荷同形）
Issue = dict[str, str | list[str]]


class FieldConfig:
    """字段表驱动的插件配置基类（标准 Schema + ini 双向转换）。

    子类声明 ``_FIELDS`` 与同名类属性即可获得完整配置模型；专属校验写进
    :meth:`_validate_extra`。用法见模块文档。
    """

    #: 字段类型表：``字段名 -> (类型元组, 说明)``；驱动默认值、类型校验
    #: 与 ini 序列化
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {}
    #: 对应的 ``config.ini`` 节名；声明后接入 appconfig 的配置注入与写回
    ini_section: ClassVar[str | None] = None

    def __init__(self, **values: object) -> None:
        """按字段表填充配置；未给出的字段取类属性默认值。"""
        for name in self._FIELDS:
            setattr(self, name, values.get(name, getattr(type(self), name)))

    # -- 标准 Schema 协议 -----------------------------------------------------
    @classmethod
    def validate(cls, value: object) -> dict[str, object]:
        """校验并归一化配置（标准 Schema 的 ``validate``）。

        参数:
            value: 原始配置，允许 ``None`` / 字典 / 本类实例。

        返回:
            ``{"value": 配置实例}``；校验失败时返回 ``{"issues": [...]}``，
            cordis 会据此抛出 ``ValidationError``。
        """
        if value is None:
            value = {}
        config = value if isinstance(value, cls) else None
        if config is not None:
            value = config.to_dict()
        if not isinstance(value, dict):
            return {
                "issues": [
                    {
                        "message": f"配置必须是字典或 {cls.__name__}，收到 {type(value).__name__}",
                        "path": [],
                    }
                ]
            }
        values = cast(dict[str, object], value)

        issues: list[Issue] = []
        for name, raw in values.items():
            if name not in cls._FIELDS:
                issues.append(
                    {
                        "message": f"未知配置项 {name}（可选项：{', '.join(sorted(cls._FIELDS))}）",
                        "path": [name],
                    }
                )
                continue
            expected, label = cls._FIELDS[name]
            # bool 是 int 的子类，需要单独排除
            if isinstance(raw, bool) and bool not in expected:
                issues.append({"message": f"{label} 需要 {_describe(expected)}", "path": [name]})
                continue
            if not isinstance(raw, expected):
                issues.append({"message": f"{label} 需要 {_describe(expected)}", "path": [name]})

        if issues:
            return {"issues": issues}

        if config is None:
            config = cls(**values)
        issues = config._validate_extra()
        return {"issues": issues} if issues else {"value": config}

    def _validate_extra(self) -> list[Issue]:
        """子类专属校验（取值范围、必填项、归一化等）；默认无额外约束。"""
        return []

    # -- 配置合并（供 Service.__resolve_config__ 与 internal/config 使用） ----
    @classmethod
    def merge(cls, *configs: object) -> Self:
        """按「前者为底、后者覆盖」的顺序合并多层配置。"""
        merged: dict[str, object] = {}
        for config in configs:
            if config is None:
                continue
            if isinstance(config, cls):
                merged.update({name: getattr(config, name) for name in cls._FIELDS})
            elif isinstance(config, dict):
                merged.update(
                    {
                        name: item
                        for name, item in cast(dict[str, object], config).items()
                        if name in cls._FIELDS
                    }
                )
        return cls(**merged)

    # -- ini 双向转换 ---------------------------------------------------------
    @classmethod
    def coerce_ini(cls, values: Mapping[str, str]) -> dict[str, object]:
        """把 ini 键值（全字符串）按 ``_FIELDS`` 类型收敛为类型化字典。

        转换失败的键**保留原字符串**，交由 :meth:`validate` 报告统一的类型
        问题；未知键原样传递，同样交给 :meth:`validate` 报告。
        """
        converted: dict[str, object] = {}
        for key, raw in values.items():
            name = str(key).lower()
            entry = cls._FIELDS.get(name)
            if entry is None:
                converted[name] = raw
                continue
            converted[name] = _coerce_ini_value(raw, entry[0])
        return converted

    @classmethod
    def from_ini(cls, values: Mapping[str, str]) -> Self:
        """从 ini 的节值（全字符串）构建并校验配置。

        抛出:
            ValueError: 出现未知键、数值无法转换或校验不通过。
        """
        result = cls.validate(cls.coerce_ini(values))
        if "issues" in result:
            messages = "; ".join(
                str(issue.get("message", ""))
                for issue in cast(list[Issue], result["issues"])
            )
            label = f"[{cls.ini_section}] 节" if cls.ini_section else "配置"
            raise ValueError(f"{label}配置无效：{messages}")
        return cast(Self, result["value"])

    @classmethod
    def to_ini(cls, value: object) -> dict[str, str]:
        """把配置（实例 / 部分字典）序列化为 ini 键值字符串。

        只序列化**实际给出**的键（部分更新 = 部分写回）；``None`` 返回空字典。
        """
        if value is None:
            return {}
        if isinstance(value, cls):
            items: dict[str, object] = value.to_dict()
        elif isinstance(value, Mapping):
            items = {
                str(key).lower(): item
                for key, item in cast(Mapping[str, object], value).items()
                if str(key).lower() in cls._FIELDS
            }
        else:
            return {}
        return {name: _stringify_ini(item) for name, item in items.items() if item is not None}

    # -- 便利方法 -------------------------------------------------------------
    def to_dict(self) -> dict[str, object]:
        """导出为字典（便于日志、自省与层合并）。"""
        return {name: getattr(self, name) for name in self._FIELDS}


def _describe(expected: tuple[type[object], ...]) -> str:
    """把类型元组渲染成人类可读的说明（``str | None`` 之类）。"""
    return " 或 ".join(t.__name__ for t in expected)


def _coerce_ini_value(raw: object, expected: tuple[type[object], ...]) -> object:
    """按期望类型把 ini 字符串收敛为类型化值；失败保留原值（宽松，交给校验报告）。"""
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if bool in expected:
        return _to_bool(text)
    if float in expected:
        return _to_number(float, text)
    if int in expected:
        return _to_number(int, text)
    if list in expected or tuple in expected:
        return [part.strip() for part in text.split(",") if part.strip()]
    return text


def _to_number(converter: type[int | float], raw: str) -> object:
    """把字符串转成数值；失败保留原值，由 ``validate`` 报类型问题。"""
    try:
        return converter(raw)
    except (ValueError, OverflowError):
        return raw


def _to_bool(raw: str) -> object:
    """把 ini 布尔写法转成 ``bool``；写法不合法保留原值（不区分大小写）。"""
    word = raw.lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    return raw


def _stringify_ini(value: object) -> str:
    """把类型化值序列化为 ini 值字符串（``to_ini`` 的单值口径）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        items = cast("list[object] | tuple[object, ...]", value)
        return ", ".join(str(item) for item in items)
    return str(value)


__all__: list[str] = ["FieldConfig", "Issue"]
