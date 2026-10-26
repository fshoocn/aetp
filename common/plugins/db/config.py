"""数据库插件配置（``config.ini`` 的 ``[db]`` 节）。

连接串是唯一换库入口：``url`` 改成目标库的 SQLAlchemy 异步连接串即可
（如 ``sqlite+aiosqlite:///data/aetp.db`` →
``postgresql+asyncpg://user:pass@host/aetp``），调用方代码零改动。

支持 ``url = env:DB_URL``：从环境变量取连接串，凭证不落 ``config.ini``；
展示（:meth:`DbConfig.to_dict`）时密码掩码为 ``***``，不回显凭证。
"""

from __future__ import annotations

import os
import re
from typing import ClassVar

from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from common.config_base import FieldConfig, Issue

#: ``env:VAR`` 形式的连接串：从环境变量取真实连接串
_ENV_URL: re.Pattern[str] = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
#: 密码掩码兜底正则（连接串解析失败时按 ``user:***@`` 掩码）
_MASKED_URL: re.Pattern[str] = re.compile(r"(://[^:/@\s]+):[^@\s]*@")

#: 孤儿表处置策略：告警不删 / 直接清理
ORPHAN_POLICIES: frozenset[str] = frozenset({"warn", "drop"})


def mask_url(url: str) -> str:
    """连接串密码掩码（``user:pass@`` → ``user:***@``），配置展示专用。"""
    raw = url.strip()
    if not raw or raw.startswith("env:"):
        return raw
    try:
        return make_url(raw).render_as_string(hide_password=True)
    except (SQLAlchemyError, ValueError):
        return _MASKED_URL.sub(r"\1:***@", raw)


class DbConfig(FieldConfig):
    """``[db]`` 节配置模型（标准 Schema + ini 双向转换）。"""

    ini_section: ClassVar[str | None] = "db"
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "url": ((str,), "SQLAlchemy 异步连接串（env:VAR 表示从环境变量取）"),
        "echo": ((bool,), "SQL 日志"),
        "pool_size": ((int,), "连接池大小"),
        "max_overflow": ((int,), "池外溢出上限"),
        "pool_recycle": ((int,), "连接回收秒数（防 MySQL wait_timeout）"),
        "orphan_policy": ((str,), "孤儿表处置：warn / drop"),
        "schema_log_limit": ((int,), "结构变更日志保留上限"),
    }

    #: SQLAlchemy 异步连接串（唯一换库入口）；``env:VAR`` 从环境变量取
    #: （SQLite 相对库路径按**节点目录**——config.ini 所在目录——解析）
    #: 默认落点按专属数据目录约定：``data/plugins/db/aetp.db``
    url: str = "sqlite+aiosqlite:///data/plugins/db/aetp.db"
    #: 是否打印 SQL 日志
    echo: bool = False
    #: 连接池大小（非 SQLite 生效）
    pool_size: int = 5
    #: 连接池溢出上限（非 SQLite 生效）
    max_overflow: int = 10
    #: 连接回收秒数（非 SQLite 生效）
    pool_recycle: int = 300
    #: 孤儿表处置策略：``warn``（自检只报告）/ ``drop``（自检时清理）
    orphan_policy: str = "warn"
    #: 库内结构变更日志（``aetp_schema_log``）保留上限，防无限增长
    schema_log_limit: int = 500

    def _validate_extra(self) -> list[Issue]:
        issues: list[Issue] = []
        url = self.url.strip()
        if not url:
            issues.append({"message": "url 不能为空", "path": ["url"]})
        elif url.startswith("env:") and not _ENV_URL.fullmatch(url):
            issues.append({"message": "url 的环境变量写法应为 env:VAR（VAR 为变量名）", "path": ["url"]})
        elif not url.startswith("env:"):
            try:
                make_url(url)
            except (SQLAlchemyError, ValueError) as exc:
                # 不回显异常原文：解析错误的消息可能内嵌整条连接串（含账密）
                issues.append(
                    {
                        "message": f"url 不是合法的 SQLAlchemy 连接串（{type(exc).__name__}）",
                        "path": ["url"],
                    }
                )
        if self.pool_size < 1:
            issues.append({"message": f"pool_size 需 ≥ 1，收到 {self.pool_size}", "path": ["pool_size"]})
        if self.max_overflow < 0:
            issues.append({"message": f"max_overflow 需 ≥ 0，收到 {self.max_overflow}", "path": ["max_overflow"]})
        if self.pool_recycle < 0:
            issues.append({"message": f"pool_recycle 需 ≥ 0，收到 {self.pool_recycle}", "path": ["pool_recycle"]})
        if self.schema_log_limit < 1:
            issues.append(
                {"message": f"schema_log_limit 需 ≥ 1，收到 {self.schema_log_limit}", "path": ["schema_log_limit"]}
            )
        if self.orphan_policy not in ORPHAN_POLICIES:
            issues.append(
                {
                    "message": f"orphan_policy 只能是 {' / '.join(sorted(ORPHAN_POLICIES))}，收到 {self.orphan_policy!r}",
                    "path": ["orphan_policy"],
                }
            )
        return issues

    def resolved_url(self) -> str:
        """解析后的连接串：``env:VAR`` 展开为环境变量值（变量缺失即报错）。

        抛出:
            ValueError: ``env:VAR`` 对应的环境变量未设置或为空。
        """
        raw = self.url.strip()
        if not raw.startswith("env:"):
            return raw
        value = os.environ.get(raw[4:], "").strip()
        if not value:
            raise ValueError(f"环境变量 {raw[4:]} 未设置或为空（[db] url = {raw}）")
        return value

    def to_dict(self) -> dict[str, object]:
        """导出为字典（展示用途）：连接串密码掩码，不回显凭证。"""
        values = self.raw_dict()
        values["url"] = mask_url(str(values.get("url") or ""))
        return values
