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
import os
import re
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

from common.config_base import ensure_ini_safe

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

    # -- 写回 ---------------------------------------------------------------
    def update_section(
        self,
        name: str,
        values: Mapping[str, str],
        *,
        path: str | Path | None = None,
    ) -> None:
        """更新配置节键值并写回 ini 文件（外科手术式行编辑，保留注释）。

        键统一按小写归一（与 ``configparser`` 的读取口径一致）：文件里已有
        的键就地替换，新键插到该节末尾，节不存在则在文件尾追加新节；
        其余行（注释、排版、其它节）一字不动。

        参数:
            name: 节名（区分大小写）。
            values: ``{键: 值}``，值需是字符串。
            path: 写回目标文件；缺省用 :attr:`path`（写回后同步为来源文件）。

        抛出:
            ValueError: 未给 ``path`` 且实例没有来源文件（如空配置）。
        """
        target = Path(path).expanduser() if path is not None else self.path
        if target is None:
            raise ValueError("配置对象没有来源文件，update_section 需要显式传入 path")
        updates = {str(key).lower(): str(value) for key, value in values.items()}
        current = self._sections.get(name, ConfigSection(name, {})).to_dict()
        write_ini_values(target, {name: updates})
        # 写回成功后才改内存：兜底过滤拒绝 / 写盘失败时内存与文件保持一致
        self._sections[name] = ConfigSection(name, {**current, **updates})
        self.path = target

    def to_dict(self) -> dict[str, dict[str, str]]:
        """导出为 ``{节: {键: 值}}``。"""
        return {name: section.to_dict() for name, section in self._sections.items()}

    def __repr__(self) -> str:
        return f"AppConfig({self.path}, sections={self.sections()})"


#: ini 节头：``[name]``（行尾可跟注释，兼容 configparser 的宽容口径）
_SECTION_HEADER: re.Pattern[str] = re.compile(r"^\s*\[([^\[\]]+)\]")
#: ini 键行：``key = value`` / ``key : value``（键名宽容匹配）
_KEY_LINE: re.Pattern[str] = re.compile(r"^\s*([^=:\s][^=:]*?)\s*[=:]")


def write_ini_values(
    path: str | Path, changes: Mapping[str, Mapping[str, str]]
) -> None:
    """把 ``{节: {键: 值}}`` 外科手术式写回 ini 文件，保留注释与排版。

    与 ``configparser`` 的整表序列化不同，本函数只改被点名的键值行：已有键
    就地替换，新键插到该节末尾，节不存在则在文件尾追加新节；文件其余行
    一字不动，因此手工维护的注释不会丢。

    写入采用「临时文件 + 原子替换」：中途崩溃不会留下半截文件；带 BOM 的
    文件按 ``utf-8-sig`` 读入（剥除 BOM），避免 BOM 挡住首行节头匹配导致
    同一节被重复追加。

    参数:
        path: ini 文件路径；文件不存在时按新文件创建。
        changes: ``{节名: {键: 值}}``；值需是字符串（:meth:`FieldConfig.to_ini`
            的产出口径）。

    抛出:
        ValueError: 值含换行或控制字符（会破坏 ini 结构，形成配置注入）。
    """
    for section, values in changes.items():
        for key, value in values.items():
            ensure_ini_safe(f"[{section}] {key}", value)
    resolved = Path(path).expanduser()
    raw = resolved.read_bytes().decode("utf-8-sig") if resolved.is_file() else ""
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines()
    for section, values in changes.items():
        lines = _apply_section(lines, section, values)
    text = newline.join(lines)
    if lines:
        text += newline
    _atomic_write(resolved, text.encode("utf-8"))


def _atomic_write(path: Path, payload: bytes) -> None:
    """临时文件 + ``os.replace`` 原子替换；失败清理临时文件，目标文件保持原状。"""
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _apply_section(
    lines: list[str], section: str, values: Mapping[str, str]
) -> list[str]:
    """在行列表里更新一个节的键值（见 :func:`write_ini_values` 的语义）。"""
    start: int | None = None
    end = len(lines)
    for index, line in enumerate(lines):
        match = _SECTION_HEADER.match(line)
        if match is None:
            continue
        if start is not None:
            end = index
            break
        if match.group(1).strip() == section:
            start = index + 1

    if start is None:
        addition = [f"[{section}]"]
        addition += [f"{key} = {value}" for key, value in values.items()]
        if lines and lines[-1].strip():
            lines.append("")
        return lines + addition

    updated = list(lines)
    body = updated[start:end]
    for key, value in values.items():
        for offset, line in enumerate(body):
            match = _KEY_LINE.match(line)
            if match is not None and match.group(1).strip().lower() == key.lower():
                body[offset] = f"{match.group(1).strip()} = {value}"
                break
        else:
            insert_at = len(body)
            while insert_at > 0 and not body[insert_at - 1].strip():
                insert_at -= 1
            body.insert(insert_at, f"{key} = {value}")
    return updated[:start] + body + updated[end:]


__all__: list[str] = ["AppConfig", "ConfigSection", "write_ini_values"]
