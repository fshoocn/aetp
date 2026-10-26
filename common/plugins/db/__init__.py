"""内置数据库服务插件（``common/plugins/db``）：统一数据入口。

对外契约（DESIGN §3）——共享契约在**非插件公共层**（插件铁律）::

    from common.models import Model, Mapped, mapped_column

    class TestRun(Model):
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]

插件声明 ``inject = ["db"]`` 后经 ``ctx.db`` 建表（``sync_schema``）与
增删改查（``repo`` / ``session`` / ``transaction``）；表归属调用方插件，
卸载即清理（DESIGN §4）。详细设计见同目录 ``README.md``。
"""

from __future__ import annotations

from .config import DbConfig
from .plugin import DbPlugin
from .service import DbDependencyError, DbError, DbService, MigrationStep, Repo, SyncResult
from .ui_service import DbUiService

__all__: list[str] = [
    "DbConfig",
    "DbDependencyError",
    "DbError",
    "DbPlugin",
    "DbService",
    "DbUiService",
    "MigrationStep",
    "Repo",
    "SyncResult",
]
