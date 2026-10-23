"""db 服务错误契约（非插件公共层）。

插件铁律：插件之间不得直接 import——``ctx.db`` 抛出的异常类型属跨服务
边界契约，住本层；捕获方直接 ``from common.db_errors import DbError``，
``common.plugins.db.service`` 保留再导出。
"""

from __future__ import annotations


class DbError(RuntimeError):
    """数据库服务层错误（仓储 / 结构同步的统一包装；逃生舱透传 SQLAlchemy 异常）。"""


class DbDependencyError(DbError):
    """跨插件数据依赖不满足（未声明 / 结构不符 / 依赖数据不存在）。"""


__all__: list[str] = ["DbDependencyError", "DbError"]
