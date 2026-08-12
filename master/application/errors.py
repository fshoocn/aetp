"""应用层业务异常。

应用服务只抛出这些与业务语义有关的异常，不依赖 FastAPI 或 HTTP 状态码。
API 层负责将它们转换为 HTTP 响应，后台任务和 CLI 可以复用同一套异常。
"""

from __future__ import annotations


class ApplicationError(Exception):
    """应用层业务错误基类。"""


class ProjectMemberError(ApplicationError):
    """项目成员操作失败。"""


class ProjectNotFoundError(ProjectMemberError):
    """项目不存在。"""


class MemberNotFoundError(ProjectMemberError):
    """项目成员不存在。"""


class MemberAlreadyExistsError(ProjectMemberError):
    """用户已经是项目成员。"""


class LastOwnerError(ProjectMemberError):
    """不能删除或降级最后一个 owner。"""


class InactiveUserError(ProjectMemberError):
    """目标用户未激活。"""


class InvalidRoleGrantError(ProjectMemberError):
    """当前操作者无权授予目标角色。"""


class NodeNotFoundError(ApplicationError):
    """节点或设备不存在。"""


class DeviceNotFoundError(ApplicationError):
    """设备不存在或不属于当前项目可用节点。"""


class NodeDisabledError(ApplicationError):
    """节点已禁用，不能绑定到项目。"""


class NodeBindingNotFoundError(ApplicationError):
    """项目节点绑定不存在。"""


class NodeAlreadyBoundError(ApplicationError):
    """节点已经绑定到项目。"""


class UsernameAlreadyExistsError(ApplicationError):
    """注册用户名已存在。"""


class InvalidCredentialsError(ApplicationError):
    """用户名或密码错误（认证失败统一口径）。"""


class ProjectKeyAlreadyExistsError(ApplicationError):
    """项目标识 project_key 已存在。"""


class InvalidProjectOwnerError(ApplicationError):
    """项目 owner 不存在或账户未激活。"""


class TaskNotFoundError(ApplicationError):
    """任务不存在。"""
