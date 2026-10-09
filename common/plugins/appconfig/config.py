"""应用配置：解析 ``config.ini``，生成对应的配置类。

不依赖 cordis 与具体插件，可独立使用::

    from common.plugins.appconfig import AppConfig

    config = AppConfig.from_file("config.ini")
    config.get("web", "port")            # "8080"
    config.get_int("web", "port")        # 8080
    config.section("node").name          # 属性式取键

约定：节（``[section]``）名**区分大小写**，键统一转**小写**（configparser 默认）；
值始终是字符串，需要数值 / 布尔时用 ``get_int`` / ``get_float`` / ``get_bool``。
"""

from __future__ import annotations

import configparser
from collections.abc import Iterator, Mapping
from pathlib import Path

#: 布尔值的合法写法（不区分大小写）
_TRUE_WORDS: frozenset[str] = frozenset({"1", "yes", "true", "on"})
_FALSE_WORDS: frozenset[str] = frozenset({"0", "no", "false", "off"})


class ConfigSection:
    """单个配置节（``[section]``）的视图：字典 / 属性两种取值方式。

    ``section.name`` 取**键** ``name`` 的值；节自己的名字在
    :attr:`section_name`（避免与常见键名 ``name`` 撞车）。
    """

    def __init__(self, name: str, values: Mapping[str, str]) -> None:
        self.section_name: str = name
        self._values: dict[str, str] = dict(values)

    # -- 取值 ---------------------------------------------------------------
    def get(self, key: str, fallback: str | None = None) -> str | None:
        """按键取字符串值；不存在返回 ``fallback``。"""
        return self._values.get(key, fallback)

    def get_int(self, key: str, fallback: int | None = None) -> int | None:
        """按键取整数值；值不是整数抛 :class:`ValueError`。"""
        raw = self._values.get(key)
        if raw is None:
            return fallback
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise ValueError(
                f"配置 {self.section_name}.{key} 不是整数：{raw!r}"
            ) from exc

    def get_float(self, key: str, fallback: float | None = None) -> float | None:
        """按键取浮点值；值不是数字抛 :class:`ValueError`。"""
        raw = self._values.get(key)
        if raw is None:
            return fallback
        try:
            return float(raw.strip())
        except ValueError as exc:
            raise ValueError(
                f"配置 {self.section_name}.{key} 不是数字：{raw!r}"
            ) from exc

    def get_bool(self, key: str, fallback: bool | None = None) -> bool | None:
        """按键取布尔值（``1/yes/true/on`` 与 ``0/no/false/off``）。"""
        raw = self._values.get(key)
        if raw is None:
            return fallback
        word = raw.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        raise ValueError(f"配置 {self.section_name}.{key} 不是布尔值：{raw!r}")

    # -- 字典 / 属性访问 -----------------------------------------------------
    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __getattr__(self, key: str) -> str:
        """属性式取键：``section.name`` 等价 ``section["name"]``。"""
        try:
            return self._values[key]
        except KeyError:
            raise AttributeError(
                f"配置节 {self.section_name!r} 没有键 {key!r}"
            ) from None

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def __iter__(self) -> Iterator[str]:
        """迭代节内全部键（支持 ``for key in section``）。"""
        return iter(self._values)

    def keys(self) -> list[str]:
        """节内全部键（小写）。"""
        return list(self._values)

    def to_dict(self) -> dict[str, str]:
        """导出为普通字典。"""
        return dict(self._values)

    def __repr__(self) -> str:
        return f"ConfigSection({self.section_name!r}, {self._values})"


class AppConfig:
    """``config.ini`` 对应的配置类：节的集合 + 类型化取值。

    由 :meth:`from_file` 从 ini 文件生成，或 ``AppConfig()`` 得到空配置
    （文件缺失时的降级形态）。
    """

    def __init__(
        self,
        sections: Mapping[str, Mapping[str, str]] | None = None,
        *,
        path: Path | None = None,
    ) -> None:
        self.path: Path | None = path
        self._sections: dict[str, ConfigSection] = {
            name: ConfigSection(name, values) for name, values in (sections or {}).items()
        }

    @classmethod
    def from_file(cls, path: str | Path) -> AppConfig:
        """从 ini 文件生成配置类实例。

        参数:
            path: ini 文件路径（相对路径按当前工作目录解析）。

        抛出:
            FileNotFoundError: 文件不存在。
            ValueError: 文件不是合法 ini。
        """
        resolved = Path(path).expanduser()
        if not resolved.is_file():
            raise FileNotFoundError(f"配置文件不存在：{resolved}")
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(resolved, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError) as exc:
            raise ValueError(f"配置文件解析失败：{resolved}：{exc}") from exc
        sections = {name: dict(parser.items(name)) for name in parser.sections()}
        return cls(sections, path=resolved)

    # -- 取值 ---------------------------------------------------------------
    def section(self, name: str) -> ConfigSection:
        """取配置节视图；不存在返回空节（配合 fallback 用法最顺手）。"""
        return self._sections.get(name, ConfigSection(name, {}))

    def sections(self) -> list[str]:
        """全部节名。"""
        return list(self._sections)

    def get(self, section: str, key: str, fallback: str | None = None) -> str | None:
        """按键取字符串值；节或键不存在返回 ``fallback``。"""
        return self.section(section).get(key, fallback)

    def get_int(
        self, section: str, key: str, fallback: int | None = None
    ) -> int | None:
        """按键取整数值；值不是整数抛 :class:`ValueError`。"""
        return self.section(section).get_int(key, fallback)

    def get_float(
        self, section: str, key: str, fallback: float | None = None
    ) -> float | None:
        """按键取浮点值；值不是数字抛 :class:`ValueError`。"""
        return self.section(section).get_float(key, fallback)

    def get_bool(
        self, section: str, key: str, fallback: bool | None = None
    ) -> bool | None:
        """按键取布尔值；写法不合法抛 :class:`ValueError`。"""
        return self.section(section).get_bool(key, fallback)

    def to_dict(self) -> dict[str, dict[str, str]]:
        """导出为 ``{节: {键: 值}}``。"""
        return {name: section.to_dict() for name, section in self._sections.items()}

    def __repr__(self) -> str:
        return f"AppConfig({self.path}, sections={self.sections()})"


__all__: list[str] = ["AppConfig", "ConfigSection"]
