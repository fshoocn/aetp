"""P8.1：Run 取消服务测试。

验收要点（§5.4/§8.4）：
1. 可取消状态（dispatched/acked/running）的 Run 向活跃 Shard 节点发 run.cancel outbox
2. 已终态 Run 取消幂等（不发 outbox，返回 already_terminal）
3. 不存在的 Run 返回 422
"""

from __future__ import annotations

import asyncio


def _create_admin(client, username="cancel-admin", password="admin-pass-123", display_name="CA") -> dict[str, str]:
    service = client.app.state.container.auth_service()
    service.bootstrap_admin(username, password, display_name)
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_project(client, headers, key="CANCEL"):
    resp = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"project_key": key, "name": key},
    )
    assert resp.status_code == 201
    return resp.json()["project_id"]


def test_cancel_nonexistent_run_returns_422(client):
    """取消不存在的 Run 返回 422。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "CANCEL-404")

    resp = client.post(
        f"/api/v1/projects/{project_id}/runs/R-NONEXIST/cancel",
        headers=headers,
    )
    assert resp.status_code == 422


def test_cancel_requires_operator_permission(client):
    """viewer 不能取消 Run。"""
    from sqlalchemy import text

    admin_headers = _create_admin(client, "cancel-admin2", "CANCEL-PERM")
    project_id = _create_project(client, admin_headers, "CANCEL-PERM")

    # 创建 viewer 用户
    svc = client.app.state.container.auth_service()
    user = svc.create_user("cancel-viewer", "pass123", "cancel-viewer")
    with client.app.state.container.database().session_scope() as session:
        session.execute(text("UPDATE users SET account_status='active' WHERE username='cancel-viewer'"))
    client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
        json={"user_id": user.id, "project_role": "viewer"},
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "cancel-viewer", "password": "pass123"},
    )
    viewer_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post(
        f"/api/v1/projects/{project_id}/runs/R-ANY/cancel",
        headers=viewer_headers,
    )
    assert resp.status_code == 403


def test_cancel_targets_current_attempt_and_is_idempotent(client):
    """取消使用活动 Attempt 节点，并且同一节点只写一个 outbox。"""
    from tests.test_p6_4_end_to_end import _seed

    container = client.app.state.container
    user_id, task_id = _seed(container)
    run_id = asyncio.run(
        container.run_trigger_service().trigger(
            task_id,
            project_id="p1",
            triggered_by_user_id=user_id,
        )
    ).run_id

    first = container.run_cancel_service().cancel(run_id, project_id="p1")
    second = container.run_cancel_service().cancel(run_id, project_id="p1")
    assert first.cancelled_shards == 2
    assert second.cancelled_shards == 2

    with container.uow_factory()() as uow:
        outbox = uow.outbox_messages.get_by_outbox_id(f"run-cancel:{run_id}:node-a")
        assert outbox is not None
