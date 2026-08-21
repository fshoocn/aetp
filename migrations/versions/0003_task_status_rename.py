"""任务状态命名统一迁移（P3.1 / D-22）。

旧值 -> 新值映射：
    dispatched / accepted -> dispatching
    completed             -> succeeded
    timeout               -> timed_out
    （新增 cancelling，无旧值对应）

实现要点（SQLite 表级 CHECK 无法临时禁用，采用三段式）：
    1. CHECK 放宽为"新旧值并集"（batch 重建表，旧数据可通过新 CHECK 校验）；
    2. 数据回填为新值（并集 CHECK 下合法）；
    3. CHECK 收紧为新值集合（batch 重建表，新数据可通过校验）。
downgrade 为有损合并：cancelling 并入 cancelled，无法还原中间态。

数据库实际约束名为命名约定生成的 ck_tasks_ck_tasks_status
（ck_<表>_<约束名>，见 orm/base.py NAMING_CONVENTION）。

Revision ID: 0003_task_status_rename
Revises: 0002_refresh_tokens
Create Date: 2026-08-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_task_status_rename"
down_revision: str | None = "0002_refresh_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# batch 操作会再次应用命名约定，因此这里传基础 token：
# ck_<表>_<token> -> ck_tasks_ck_tasks_status（与 0001/ORM 生成的库内名一致）
_DB_CHECK_NAME = "ck_tasks_status"

_NEW_CHECK = "status IN ('pending','dispatching','running','cancelling','succeeded','failed','cancelled','timed_out')"
_OLD_CHECK = "status IN ('pending','dispatched','accepted','running','completed','failed','cancelled','timeout')"
_UNION_CHECK = (
    "status IN ('pending','dispatching','running','cancelling',"
    "'succeeded','failed','cancelled','timed_out',"
    "'dispatched','accepted','completed','timeout')"
)


def _swap_check(new_check: str) -> None:
    """batch 重建表并替换 CHECK 约束（SQLite 安全，自动处理外键重指向）。"""
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(_DB_CHECK_NAME, type_="check")
        batch_op.create_check_constraint("ck_tasks_status", new_check)


def upgrade() -> None:
    # 1. CHECK 放宽为并集（旧数据在重建拷贝时可通过新约束）
    _swap_check(_UNION_CHECK)
    # 2. 数据回填为新值（并集 CHECK 下合法）
    op.execute("UPDATE tasks SET status='dispatching' WHERE status IN ('dispatched','accepted')")
    op.execute("UPDATE tasks SET status='succeeded' WHERE status='completed'")
    op.execute("UPDATE tasks SET status='timed_out' WHERE status='timeout'")
    # 3. CHECK 收紧为新值集合
    _swap_check(_NEW_CHECK)


def downgrade() -> None:
    # 1. CHECK 放宽为并集
    _swap_check(_UNION_CHECK)
    # 2. 数据还原（cancelling -> cancelled 为有损合并）
    op.execute("UPDATE tasks SET status='dispatched' WHERE status='dispatching'")
    op.execute("UPDATE tasks SET status='completed' WHERE status='succeeded'")
    op.execute("UPDATE tasks SET status='timeout' WHERE status='timed_out'")
    op.execute("UPDATE tasks SET status='cancelled' WHERE status='cancelling'")
    # 3. CHECK 恢复为旧值集合
    _swap_check(_OLD_CHECK)
