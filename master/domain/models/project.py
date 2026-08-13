"""领域对象：项目。

项目是任务、成员和节点权限的业务边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from master.domain.enums import ProjectRole, ProjectStatus


@dataclass
class Project:
    """测试项目。

    id: 内部代理主键（持久化后填充）
    project_id: 对外业务 ID（如 P-...），全局唯一
    project_key: 用户可读的项目标识，全局唯一
    """

    id: int | None
    project_id: str
    project_key: str
    name: str
    description: str
    status: ProjectStatus
    created_by: int
    created_at: datetime
    updated_at: datetime
    # 仅用于“当前用户可见项目”查询的权限投影，管理员列表为空。
    project_role: ProjectRole | None = None
