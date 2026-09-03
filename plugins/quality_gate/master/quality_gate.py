"""Quality Gate 准入 Hook 样例插件。

在 Run 创建阶段（stage=run.before_create）评估载荷；示例只做演示：默认放行，
若载荷带 ``block=true`` 则拒绝。插件只依赖 aetp_protocol.hooks，不触碰 Kernel。
"""

from __future__ import annotations

from aetp_protocol.hooks import AdmissionHookPlugin, HookAdmission, HookEvaluation


class QualityGateHook:
    name = "org.example.quality-gate"
    stage = "run.before_create"
    order = 100

    async def evaluate(self, evaluation: HookEvaluation) -> HookAdmission:
        block = evaluation.payload.get("block") is True
        if block:
            return HookAdmission(
                allowed=False,
                reason="载荷标记 block=true，准入拒绝（示例）",
                code="QUALITY_GATE_BLOCKED",
            )
        return HookAdmission(allowed=True)


def create_hook() -> AdmissionHookPlugin:
    return QualityGateHook()


__all__ = ["QualityGateHook", "create_hook"]
