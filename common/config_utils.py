"""AETP 共享配置工具（Master/Agent 共用）。

仅包含与具体组件无关的纯函数，供各组件 ``config.py`` 复用，避免重复：

- ``load_env_file``：极简 .env 解析器（KEY=VALUE、# 注释、成对引号）
- ``parse_bool`` / ``parse_int``：标量类型解析（空值回退默认）
- ``parse_task_types``：逗号分隔列表解析
- ``resolve_sqlite_url``：SQLite 相对路径基于给定基准目录解析为绝对连接串
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


def parse_task_types(value: str | None) -> tuple[str, ...]:
    """逗号分隔的任务类型列表解析。"""
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


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
