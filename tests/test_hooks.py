"""P8.4：生命周期 Hook 框架测试。

验收要点（§10.4/§10.6）：
1. 准入 Hook 按 (order, name) 稳定排序
2. deny 拒绝操作，返回机器可读错误码
3. 超时映射为 HOOK_TIMEOUT
4. 事件 Hook fail open：异常不回滚业务
5. hook_executions 审计记录
"""

from __future__ import annotations

import asyncio

from master.domain.hooks import HookContext, HookDecision
from master.domain.models import DomainEvent


def _create_admin(client, username="hook-admin", password="admin-pass-123") -> dict[str, str]:
    service = client.app.state.container.auth_service()
    service.bootstrap_admin(username, password, username)
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_project(client, headers, key="HOOK"):
    resp = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"project_key": key, "name": key},
    )
    assert resp.status_code == 201
    return resp.json()["project_id"]


class _AllowHook:
    name = "test-allow"
    stage = "run.before_create"
    order = 10

    async def evaluate(self, context: HookContext) -> HookDecision:
        return HookDecision(allowed=True)


class _DenyHook:
    name = "test-deny"
    stage = "run.before_create"
    order = 1

    async def evaluate(self, context: HookContext) -> HookDecision:
        return HookDecision(
            allowed=False,
            reason="测试拒绝",
            code="TEST_HOOK_DENIED",
        )


class _TimeoutHook:
    name = "test-timeout"
    stage = "run.before_create"
    order = 5

    async def evaluate(self, context: HookContext) -> HookDecision:
        await asyncio.sleep(10)
        return HookDecision(allowed=True)


class _EventHook:
    name = "test-event"
    event_types = frozenset({"run.succeeded"})

    async def handle(self, event: DomainEvent) -> None:
        pass


class _FailingEventHook:
    name = "test-failing-event"
    event_types = frozenset({"run.failed"})

    async def handle(self, event: DomainEvent) -> None:
        raise RuntimeError("Hook 异常测试")


def test_admission_hooks_sorted_by_order(client):
    """准入 Hook 按 (order, name) 稳定排序。"""
    from master.application.services.hook_runner import HookRegistry, HookRunner

    registry = HookRegistry(admission_hooks=[_AllowHook(), _DenyHook()])
    runner = HookRunner(lambda: client.app.state.container.uow_factory()(), registry=registry)

    hooks = registry.sorted_admission("run.before_create")
    assert [h.name for h in hooks] == ["test-deny", "test-allow"]


def test_admission_deny_rejects(client):
    """准入 Hook deny 拒绝操作。"""
    from master.application.services.hook_runner import HookRegistry, HookRunner

    registry = HookRegistry(admission_hooks=[_DenyHook()])
    runner = HookRunner(lambda: client.app.state.container.uow_factory()(), registry=registry)

    decision = asyncio.run(runner.run_admission(
        "run.before_create",
        HookContext(stage="run.before_create"),
    ))
    assert decision.allowed is False
    assert decision.code == "TEST_HOOK_DENIED"


def test_admission_timeout_rejects(client):
    """准入 Hook 超时映射为 HOOK_TIMEOUT。"""
    from master.application.services.hook_runner import HookRegistry, HookRunner

    registry = HookRegistry(admission_hooks=[_TimeoutHook()])
    runner = HookRunner(
        lambda: client.app.state.container.uow_factory()(),
        registry=registry,
        timeout_s=0.1,
    )

    decision = asyncio.run(runner.run_admission(
        "run.before_create",
        HookContext(stage="run.before_create"),
    ))
    assert decision.allowed is False
    assert decision.code == "HOOK_TIMEOUT"


def test_event_hook_fail_open(client):
    """事件 Hook 异常不抛出，fail open。"""
    from master.application.services.hook_runner import HookRegistry, HookRunner

    registry = HookRegistry(event_hooks=[_FailingEventHook()])
    runner = HookRunner(lambda: client.app.state.container.uow_factory()(), registry=registry)

    event = DomainEvent(
        event_id="E-1",
        project_id="P-1",
        event_type="run.failed",
        aggregate_id="R-1",
    )
    # fail open：不抛异常
    asyncio.run(runner.run_event_hooks(event))
