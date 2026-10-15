"""内置数据库服务插件（``common/plugins/db``）：统一数据入口。

对外契约（DESIGN §3）::

    from common.plugins.db import Model, Mapped, mapped_column

    class TestRun(Model):
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str)

插件声明 ``inject = ["db"]`` 后经 ``ctx.db`` 建表（``sync_schema``）与
增删改查（``repo`` / ``session`` / ``transaction``）；表归属调用方插件，
卸载即清理（DESIGN §4）。详细设计见同目录 ``DESIGN.md``。
"""

from __future__ import annotations

from .config import DbConfig
from .models import Mapped, Model, mapped_column, model_logical, model_owner, physical_name
from .plugin import DbPlugin
from .registry import DataDependency
from .service import DbDependencyError, DbError, DbService, MigrationStep, Repo, SyncResult

__all__: list[str] = [
    "DataDependency",
    "DbConfig",
    "DbDependencyError",
    "DbError",
    "DbPlugin",
    "DbService",
    "Mapped",
    "MigrationStep",
    "Model",
    "Repo",
    "SyncResult",
    "mapped_column",
    "model_logical",
    "model_owner",
    "physical_name",
]
