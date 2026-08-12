"""领域对象：项目成员。

项目成员是用户与项目的授权关系，角色仅在项目范围内生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from master.domain.enums import ProjectRole


@dataclass
class ProjectMember:
    """用户与项目的授权关系。"""

    id: int | None
    project_id: str
    user_id: int
    project_role: ProjectRole
    assigned_by: int
    created_at: datetime
    updated_at: datetime


@dataclass
class ProjectMemberWithUser:
    """项目成员及其用户信息的查询结果视图。

    由仓储通过 join 一次取出，避免 N+1 查询。
    """

    id: int
    project_id: str
    user_id: int
    username: str
    display_name: str
    project_role: ProjectRole
    assigned_by: int
    created_at: datetime
    updated_at: datetime
