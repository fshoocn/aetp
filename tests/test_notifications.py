"""P7.6：通知端点 / 事件订阅 / 投递状态 API 测试。

验收要点（§10.5）：
1. 通知端点 CRUD（owner 创建/编辑/删除，viewer 只读）
2. 密钥不回显（API 响应只含 has_secret 布尔值）
3. 事件订阅 CRUD（maintainer 创建/编辑，引用端点校验）
4. 投递记录查询与重试（owner 可重试失败投递）
5. 越权访问：viewer 不能创建端点，operator 不能管理端点
"""

from __future__ import annotations

from sqlalchemy import text


def _create_admin(client) -> dict[str, str]:
    service = client.app.state.container.auth_service()
    service.bootstrap_admin("notif-admin", "admin-pass-123", "NA")
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "notif-admin", "password": "admin-pass-123"},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_project(client, headers, key="NOTIF"):
    resp = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"project_key": key, "name": key},
    )
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _add_member(client, headers, project_id, username, password, role):
    """创建用户并添加为项目成员。"""
    svc = client.app.state.container.auth_service()
    user = svc.create_user(username, password, username)
    with client.app.state.container.database().session_scope() as session:
        session.execute(
            text("UPDATE users SET account_status='active' WHERE username=:u"),
            {"u": username},
        )
    login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=headers,
        json={"user_id": user.id, "project_role": role},
    )
    return member_headers


# ---- 通知端点 ----


def test_endpoint_crud_and_secret_masking(client):
    """通知端点 CRUD + 密钥不回显 + has_secret 标记。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers)

    # 创建端点（含密钥）
    resp = client.post(
        f"/api/v1/projects/{project_id}/notification-endpoints",
        headers=headers,
        json={
            "channel_type": "generic_webhook",
            "name": "测试 Webhook",
            "config": {"url": "https://example.com/hook"},
            "secret_value": "super-secret-token",
        },
    )
    assert resp.status_code == 201, resp.text
    ep = resp.json()
    assert ep["name"] == "测试 Webhook"
    assert ep["channel_type"] == "generic_webhook"
    assert ep["has_secret"] is True
    assert "secret_value" not in ep
    assert "secret_ref" not in ep

    endpoint_id = ep["endpoint_id"]

    # 查询端点列表
    resp = client.get(
        f"/api/v1/projects/{project_id}/notification-endpoints",
        headers=headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["has_secret"] is True

    # 编辑端点（更新名称，不带密钥）
    resp = client.patch(
        f"/api/v1/projects/{project_id}/notification-endpoints/{endpoint_id}",
        headers=headers,
        json={"name": "更新后 Webhook"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "更新后 Webhook"
    assert resp.json()["has_secret"] is True  # 保留原密钥

    # 创建无密钥端点
    resp2 = client.post(
        f"/api/v1/projects/{project_id}/notification-endpoints",
        headers=headers,
        json={
            "channel_type": "console_test",
            "name": "控制台端点",
        },
    )
    assert resp2.status_code == 201
    assert resp2.json()["has_secret"] is False

    # 删除端点
    resp = client.delete(
        f"/api/v1/projects/{project_id}/notification-endpoints/{endpoint_id}",
        headers=headers,
    )
    assert resp.status_code == 204

    # 确认已删除
    resp = client.get(
        f"/api/v1/projects/{project_id}/notification-endpoints",
        headers=headers,
    )
    assert len(resp.json()) == 1
    assert resp.json()[0]["name"] == "控制台端点"


def test_endpoint_requires_owner_permission(client):
    """viewer 不能创建端点。"""
    admin_headers = _create_admin(client)
    project_id = _create_project(client, admin_headers, "NOTIF_PERM")
    viewer_headers = _add_member(
        client, admin_headers, project_id, "viewer-user", "pass123", "viewer"
    )

    resp = client.post(
        f"/api/v1/projects/{project_id}/notification-endpoints",
        headers=viewer_headers,
        json={"channel_type": "console_test", "name": "test"},
    )
    assert resp.status_code == 403


def test_endpoint_rejects_invalid_channel(client):
    """不支持的通道类型返回 422。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "NOTIF_CHAN")

    resp = client.post(
        f"/api/v1/projects/{project_id}/notification-endpoints",
        headers=headers,
        json={"channel_type": "unsupported", "name": "bad"},
    )
    assert resp.status_code == 422
    assert "不支持的通道类型" in resp.json()["detail"]


# ---- 事件订阅 ----


def test_subscription_crud(client):
    """事件订阅创建、查询、编辑、删除。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "NOTIF_SUB")

    # 先创建端点
    ep_resp = client.post(
        f"/api/v1/projects/{project_id}/notification-endpoints",
        headers=headers,
        json={"channel_type": "console_test", "name": "sub-endpoint"},
    )
    assert ep_resp.status_code == 201
    endpoint_id = ep_resp.json()["endpoint_id"]

    # 创建订阅
    resp = client.post(
        f"/api/v1/projects/{project_id}/event-subscriptions",
        headers=headers,
        json={
            "endpoint_id": endpoint_id,
            "event_types": ["run.succeeded", "run.failed"],
        },
    )
    assert resp.status_code == 201, resp.text
    sub = resp.json()
    assert sub["endpoint_id"] == endpoint_id
    assert sub["event_types"] == ["run.succeeded", "run.failed"]
    subscription_id = sub["subscription_id"]

    # 列表
    resp = client.get(
        f"/api/v1/projects/{project_id}/event-subscriptions",
        headers=headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # 编辑事件类型
    resp = client.patch(
        f"/api/v1/projects/{project_id}/event-subscriptions/{subscription_id}",
        headers=headers,
        json={"event_types": ["run.succeeded"]},
    )
    assert resp.status_code == 200
    assert resp.json()["event_types"] == ["run.succeeded"]

    # 删除
    resp = client.delete(
        f"/api/v1/projects/{project_id}/event-subscriptions/{subscription_id}",
        headers=headers,
    )
    assert resp.status_code == 204


def test_subscription_rejects_unknown_endpoint(client):
    """引用不存在的端点返回 422。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "NOTIF_SUB_BAD")

    resp = client.post(
        f"/api/v1/projects/{project_id}/event-subscriptions",
        headers=headers,
        json={
            "endpoint_id": "NE-NONEXISTENT",
            "event_types": ["run.succeeded"],
        },
    )
    assert resp.status_code == 422
    assert "通知端点不存在" in resp.json()["detail"]


def test_subscription_rejects_empty_event_types(client):
    """空事件类型返回 422。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "NOTIF_SUB_EMPTY")
    ep_resp = client.post(
        f"/api/v1/projects/{project_id}/notification-endpoints",
        headers=headers,
        json={"channel_type": "console_test", "name": "ep"},
    )
    endpoint_id = ep_resp.json()["endpoint_id"]

    resp = client.post(
        f"/api/v1/projects/{project_id}/event-subscriptions",
        headers=headers,
        json={"endpoint_id": endpoint_id, "event_types": []},
    )
    assert resp.status_code == 422


# ---- 投递记录 ----


def _seed_delivery(client, headers, project_id):
    """直接写入投递记录用于测试。"""
    container = client.app.state.container
    with container.uow_factory()() as uow:
        from master.domain.models.notification import EventDelivery
        delivery = uow.event_deliveries.add(
            EventDelivery(
                delivery_id="DL-TEST-001",
                project_id=project_id,
                event_id="evt-001",
                subscription_id="ES-TEST",
                endpoint_id="NE-TEST",
                status="exhausted",
                attempts=5,
                error_message="连接超时",
            )
        )
    return delivery.delivery_id


def test_delivery_list_and_retry(client):
    """投递记录查询与重试。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "NOTIF_DL")
    delivery_id = _seed_delivery(client, headers, project_id)

    # 查询全部投递
    resp = client.get(
        f"/api/v1/projects/{project_id}/event-deliveries",
        headers=headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["status"] == "exhausted"

    # 按状态过滤
    resp = client.get(
        f"/api/v1/projects/{project_id}/event-deliveries?status_filter=succeeded",
        headers=headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 0

    # 重试
    resp = client.post(
        f"/api/v1/projects/{project_id}/event-deliveries/{delivery_id}/retry",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


def test_delivery_requires_owner_for_retry(client):
    """viewer 不能重试投递。"""
    admin_headers = _create_admin(client)
    project_id = _create_project(client, admin_headers, "NOTIF_DL_PERM")
    delivery_id = _seed_delivery(client, admin_headers, project_id)
    viewer_headers = _add_member(
        client, admin_headers, project_id, "dl-viewer", "pass123", "viewer"
    )

    resp = client.post(
        f"/api/v1/projects/{project_id}/event-deliveries/{delivery_id}/retry",
        headers=viewer_headers,
    )
    assert resp.status_code == 403


def test_delivery_retry_rejects_non_retryable_status(client):
    """成功状态不能重试。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "NOTIF_DL_OK")

    container = client.app.state.container
    with container.uow_factory()() as uow:
        from master.domain.models.notification import EventDelivery
        uow.event_deliveries.add(
            EventDelivery(
                delivery_id="DL-SUCCESS",
                project_id=project_id,
                event_id="evt-ok",
                subscription_id="ES-OK",
                endpoint_id="NE-OK",
                status="succeeded",
                attempts=1,
            )
        )

    resp = client.post(
        f"/api/v1/projects/{project_id}/event-deliveries/DL-SUCCESS/retry",
        headers=headers,
    )
    assert resp.status_code == 422
    assert "不允许重试" in resp.json()["detail"]
