"""ORM：测试任务定义（P3.3）。

任务定义与执行分离：test_tasks 是可复用定义，task_runs（P3.4）是一次执行。
script_pk 指向 test_scripts.id（脚本**具体版本**），FK RESTRICT 实现引用保护：
被任务定义引用的脚本版本不可物理删除。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TimestampMixin

if TYPE_CHECKING:
    from .project import Project
    from .test_script import TestScript


class TestTask(Base, TimestampMixin):
    __tablename__ = "test_tasks"
    __table_args__ = (
        UniqueConstraint("project_pk", "name", name="uq_test_tasks_project_name"),
        Index(
            "ix_test_tasks_project_enabled_created",
            "project_pk",
            "enabled",
            "created_at",
        ),
    )

    # sym:id 代理主键（自增 int），仅供内部引用；对外业务标识用 task_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:task_id 任务定义业务标识（ULID），全局唯一，对外暴露
    task_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sym:project_pk 所属项目代理主键（项目边界 D-12）
    project_pk: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # sym:script_pk 引用脚本版本的代理主键（FK RESTRICT=引用保护，§18.4 删除保护）
    # nullable: 已停用任务在脚本删除前置空，解除 FK 引用
    script_pk: Mapped[int | None] = mapped_column(
        ForeignKey("test_scripts.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # sym:task_type 任务类型（插件类型），与引用脚本的 task_type 一致
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:name 定义名，项目内唯一（(project_pk, name) 唯一约束）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # sym:default_case_selection 默认勾选用例集合（case 的 stable_key 列表，D-15）
    default_case_selection: Mapped[list] = mapped_column(
        JSONType, nullable=False, default=list
    )
    # sym:node_ids 绑定执行节点业务 ID 列表（⊆ 项目绑定节点，D-23）
    node_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    # sym:split_policy 分割策略 JSON：{type: none|by_time|by_case_count|custom, ...}（§18.6）
    split_policy: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # sym:retry_policy 重试策略 JSON：{max_attempts, failover_nodes, case_retry}（D-20）
    retry_policy: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # sym:timeout_s 任务超时秒数；0 = 不限制
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # sym:enabled 启用标记：false 时禁止触发新 Run
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # sym:priority 优先级（数值越大越优先）
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # sym:created_by 创建者（users.id），审计字段
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    # sym:project 所属项目 ORM 关系（查询时反查 project_id）
    project: Mapped[Project] = relationship()
    # sym:script 引用脚本版本 ORM 关系（查询时反查 script_id/version）
    script: Mapped[TestScript] = relationship()
