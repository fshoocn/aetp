"""应用层业务异常。

应用服务只抛出这些与业务语义有关的异常，不依赖 FastAPI 或 HTTP 状态码。
API 层负责将它们转换为 HTTP 响应，后台任务和 CLI 可以复用同一套异常。
"""

from __future__ import annotations


class ApplicationError(Exception):
    """应用层业务错误基类。"""

    # 三段式错误响应的 code（子类可覆盖；缺省由类名转 snake_case）
    error_code: str | None = None

    @property
    def code(self) -> str:
        """三段式错误码：显式声明优先，否则类名转 snake_case。"""
        if self.error_code is not None:
            return self.error_code
        name = type(self).__name__.removesuffix("Error")
        out: list[str] = []
        for ch in name:
            if ch.isupper() and out:
                out.append("_")
            out.append(ch.lower())
        return "".join(out)


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


class NodeCapabilityMismatchError(ApplicationError):
    """节点硬件能力不满足任务需求（NODE_CAPABILITY_MISMATCH，§5.5/§18.5 D-23）。

    failures 列出每个不满足条件的原因；available 列出节点实际上报的能力键，
    便于排查"需求引用了错误能力键/节点缺该能力"的错位。
    """

    # sym:error_code 机器可读错误码（§5.5）
    error_code = "NODE_CAPABILITY_MISMATCH"

    def __init__(
        self,
        node_id: str,
        failures: list[str] | tuple[str, ...] = (),
        *,
        available: list[str] | tuple[str, ...] = (),
    ) -> None:
        self.node_id = node_id
        self.failures = list(failures)
        self.available = list(available)
        detail = f"节点 {node_id} 不满足硬件要求"
        if self.failures:
            detail += f"：{'；'.join(self.failures)}"
        if self.available:
            detail += f"（节点实际能力键: {', '.join(self.available)}）"
        super().__init__(detail)


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


class RunNotFoundError(ApplicationError):
    """Run 不存在或当前用户不可见。"""


class ScriptNotFoundError(ApplicationError):
    """脚本不存在或当前用户不可见（SCRIPT_NOT_FOUND，§5.5）。"""


class ProjectAccessDeniedError(ApplicationError):
    """节点不在项目绑定范围（PROJECT_ACCESS_DENIED，§5.5/§18.5 D-23 两层绑定）。"""

    # sym:error_code 机器可读错误码（§5.5）
    error_code = "PROJECT_ACCESS_DENIED"
