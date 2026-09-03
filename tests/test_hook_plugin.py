"""PluginAdmissionHook（hook 插件 → 内部 AdmissionHook 桥接）测试。"""

from __future__ import annotations

import asyncio

from aetp_protocol.hooks import HookAdmission, HookEvaluation

from master.adapters.hooks.plugin_hook import PluginAdmissionHook
from master.domain.hooks import HookContext


class _AllowHook:
    name = "org.example.quality-gate"
    stage = "run.before_create"
    order = 100

    async def evaluate(self, evaluation: HookEvaluation) -> HookAdmission:
        del evaluation
        return HookAdmission(allowed=True)


class _DenyOnBlock:
    name = "org.example.blocker"
    stage = "run.before_create"
    order = 50

    async def evaluate(self, evaluation: HookEvaluation) -> HookAdmission:
        if evaluation.payload.get("block") is True:
            return HookAdmission(
                allowed=False,
                reason="blocked",
                code="QUALITY_GATE_BLOCKED",
            )
        return HookAdmission(allowed=True)


def test_adapter_bridges_evaluate_to_decision() -> None:
    hook = PluginAdmissionHook(_AllowHook())
    assert hook.name == "org.example.quality-gate"
    assert hook.stage == "run.before_create"
    assert hook.order == 100

    decision = asyncio.run(
        hook.evaluate(
            HookContext(
                stage="run.before_create",
                project_id="PRJ-1",
                payload={"run_id": "RUN-1"},
            )
        )
    )
    assert decision.allowed is True
    assert decision.code is None


def test_adapter_maps_deny() -> None:
    hook = PluginAdmissionHook(_DenyOnBlock())
    decision = asyncio.run(
        hook.evaluate(
            HookContext(
                stage="run.before_create",
                project_id="PRJ-1",
                payload={"block": True},
            )
        )
    )
    assert decision.allowed is False
    assert decision.code == "QUALITY_GATE_BLOCKED"
    assert decision.reason == "blocked"


def test_adapter_requires_name_stage_and_evaluate() -> None:
    class _NoStage:
        name = "x"
        order = 0

        async def evaluate(self, evaluation) -> HookAdmission:
            return HookAdmission(allowed=True)

    try:
        PluginAdmissionHook(_NoStage())
    except ValueError as exc:
        assert "stage" in str(exc)
    else:
        raise AssertionError("缺 stage 应报错")

    class _NoEval:
        name = "x"
        stage = "s"
        order = 0

    try:
        PluginAdmissionHook(_NoEval())
    except ValueError as exc:
        assert "evaluate" in str(exc)
    else:
        raise AssertionError("缺 evaluate 应报错")
