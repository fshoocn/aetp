"""ORM：脚本用例索引。

(script_pk, stable_key) 唯一；deleted 标记版本 diff 后的失效用例
（物理删除会破坏任务定义引用，故用软删除）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TimestampMixin

if TYPE_CHECKING:
    from .test_script import TestScript


class ScriptCase(Base, TimestampMixin):
    __tablename__ = "script_cases"
    __table_args__ = (
        UniqueConstraint("script_pk", "stable_key", name="uq_script_cases_script_stable_key"),
        Index("ix_script_cases_script_order", "script_pk", "order_index"),
    )

    # sym:id 代理主键（自增 int），仅供内部引用；对外业务标识用 case_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:case_id 用例业务标识（ULID），全局唯一，对外暴露
    case_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sym:script_pk 所属脚本版本代理主键，脚本删除时级联清理
    script_pk: Mapped[int] = mapped_column(
        ForeignKey("test_scripts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # sym:stable_key 脚本内稳定标识（pytest nodeid / CANoe 用例名 / cdd 路径）；
    #   (script_pk, stable_key) 唯一，跨版本 diff 与任务定义勾选引用（§18.3）
    stable_key: Mapped[str] = mapped_column(String(256), nullable=False)
    # sym:name 用例显示名
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # sym:parent_path 用例在脚本内的父路径/分组，用于树形展示
    parent_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # sym:tags 用例标签（JSON 数组），供筛选与分割策略使用
    tags: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    # sym:params 用例参数（JSON），插件执行时使用
    params: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # sym:avg_duration_s 平均耗时（秒），仅统计成功 case（D-21），by_time 分割依据；null=尚无样本
    avg_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # sym:duration_samples 耗时统计样本数
    duration_samples: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # sym:order_index 用例在脚本内的展示/执行顺序
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # sym:deleted 软删除标记：版本 diff 后失效用例置位以保留任务定义引用（§18.4 删除保护）
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # sym:script 所属脚本版本 ORM 关系（查询时反查 script_id）
    script: Mapped[TestScript] = relationship()
