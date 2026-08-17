"""AETP 共享配置工具（Master/Agent 共用）。

仅包含与具体组件无关的纯函数，供各组件 ``config.py`` 复用，避免重复：

- ``load_env_file``：极简 .env 解析器（KEY=VALUE、# 注释、成对引号）
- ``parse_bool`` / ``parse_int``：标量类型解析（空值回退默认）
- ``resolve_sqlite_url``：SQLite 相对路径基于给定基准目录解析为绝对连接串
- ``upsert_env_value``：更新或追加 .env 键值并保留其他内容
"""

from __future__ import annotations

from pathlib import Path


def load_env_file(env_file: str | Path) -> dict[str, str]:
    """极简 .env 解析器：支持 KEY=VALUE、# 注释、成对引号去除。"""
    values: dict[str, str] = {}
    path = Path(env_file)
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def upsert_env_value(env_file: str | Path, key: str, value: str) -> None:
    """更新或追加一个 .env 键值，并保留其他配置与注释。"""
    path = Path(env_file)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = text.splitlines()
    replacement = f"{key}={value}"

    for index, raw_line in enumerate(lines):
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            continue
        existing_key, _, _ = raw_line.partition("=")
        if existing_key.strip() == key:
            lines[index] = replacement
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(replacement)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_bool(value: str | None, default: bool) -> bool:
    """布尔解析；空值回退默认。"""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_int(value: str | None, default: int) -> int:
    """整数解析；空值回退默认。"""
    if value is None or value.strip() == "":
        return default
    return int(value)


def resolve_sqlite_url(url: str, base_dir: str | Path) -> str:
    """将 SQLite 相对路径基于 base_dir 解析为绝对连接串。"""
    scheme, _, rest = url.partition("://")
    if not scheme.lower().startswith("sqlite") or not rest.startswith("/"):
        return url
    relative_path = rest.lstrip("/")
    if relative_path in ("", ":memory:", ":memory"):
        return url
    path = Path(relative_path)
    if path.is_absolute():
        return url
    target = (Path(base_dir) / path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    return f"{scheme}:///{target}"
