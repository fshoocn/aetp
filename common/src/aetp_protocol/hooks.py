"""生命周期 Hook 插件契约（point=hook）。

插件实现准入策略 Hook（在 Run 等业务操作提交前评估，fail-closed 语义由 Kernel
决定）。插件只依赖本协议 DTO，不 import Kernel 内部类型；Kernel 用适配器把插件
包装成内部 ``master.domain.hooks.AdmissionHook`` 供 ``HookRunner`` 执行。

参考：规范 §5.4 AdmissionHook。payload 是触发阶段的命令/操作载荷（JSON 值），
插件不得直接读写 Kernel 状态；只返回决策。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from .ids import JsonObject


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HookEvaluation(_Strict):
    """准入 Hook 评估输入（协议化）。"""

    stage: str = Field(min_length=1)
    project_id: str | None = None
    payload: JsonObject = Field(default_factory=dict)


class HookAdmission(_Strict):
    """准入 Hook 决策输出。"""

    allowed: bool
    reason: str = ""
    code: str | None = None
    advisory: bool = False


class AdmissionHookPlugin(Protocol):
    """准入策略插件：name/stage/order 由 Kernel 注册时读取，evaluate 由 Kernel 调用。"""

    name: str
    stage: str
    order: int

    async def evaluate(self, evaluation: HookEvaluation) -> HookAdmission: ...


__all__ = ["AdmissionHookPlugin", "HookAdmission", "HookEvaluation"]
