"""准入 Hook 插件 → 内部 AdmissionHook 的适配器。

插件实现 ``aetp_protocol.hooks.AdmissionHookPlugin``（name/stage/order +
async evaluate(HookEvaluation) -> HookAdmission），本适配器包装成内部
``master.domain.hooks.AdmissionHook``（async evaluate(HookContext) -> HookDecision），
供 ``HookRunner`` 按 stage/order 执行并审计。适配器只做 DTO 转换。
"""

from __future__ import annotations

from aetp_protocol.hooks import HookAdmission, HookEvaluation
from aetp_protocol.ids import JsonObject

from master.domain.hooks import HookContext, HookDecision


def _to_json_object(value: object) -> JsonObject:
    """尽力把 payload 转成 JsonObject（跳过非 JSON 值，避免污染协议 DTO）。"""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items() if _is_json(item)}
    return {}


def _is_json(value: object) -> bool:
    return value is None or isinstance(value, (bool, int, float, str, list, dict))


class PluginAdmissionHook:
    """把协议 AdmissionHookPlugin 桥接成内部 AdmissionHook。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.name = str(getattr(plugin, "name", "")).strip()
        self.stage = str(getattr(plugin, "stage", "")).strip()
        try:
            self.order = int(getattr(plugin, "order", 0) or 0)
        except (TypeError, ValueError):
            self.order = 0
        if not self.name or not self.stage:
            raise ValueError("hook 插件缺少 name/stage")
        if not callable(getattr(plugin, "evaluate", None)):
            raise ValueError(f"hook 插件缺少 evaluate(): {self.name}")

    async def evaluate(self, context: HookContext) -> HookDecision:
        evaluation = HookEvaluation(
            stage=self.stage,
            project_id=context.project_id,
            payload=_to_json_object(context.payload),
        )
        result = await self.plugin.evaluate(evaluation)
        if not isinstance(result, HookAdmission):
            raise TypeError("hook 插件 evaluate() 必须返回 HookAdmission")
        return HookDecision(
            allowed=result.allowed,
            reason=result.reason,
            code=result.code,
            advisory=result.advisory,
        )


__all__ = ["PluginAdmissionHook"]
