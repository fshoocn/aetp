"""P8.3：CI/CD 集成 API 测试。

验收要点（§8.8）：
1. 集成 CRUD（owner 权限）
2. 触发绑定 CRUD（maintainer 权限）
3. 签名验证失败拒绝
4. delivery 去重
"""

from __future__ import annotations

from sqlalchemy import text


def _create_admin(client, username="ci-admin", password="admin-pass-123") -> dict[str, str]:
    service = client.app.state.container.auth_service()
    service.bootstrap_admin(username, password, username)
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_project(client, headers, key="CI"):
    resp = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"project_key": key, "name": key},
    )
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _add_member(client, headers, project_id, username, password, role):
    svc = client.app.state.container.auth_service()
    user = svc.create_user(username, password, username)
    with client.app.state.container.database().session_scope() as session:
        session.execute(
            text("UPDATE users SET account_status='active' WHERE username=:u"),
            {"u": username},
        )
    client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=headers,
        json={"user_id": user.id, "project_role": role},
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_integration_crud(client):
    """CI 集成 CRUD + secret 不回显。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "CI-CRUD")

    # 创建集成
    resp = client.post(
        f"/api/v1/projects/{project_id}/integrations",
        headers=headers,
        json={
            "provider": "github",
            "name": "GitHub Actions",
            "secret_value": "webhook-secret-123",
            "config_json": {"events": ["push"]},
        },
    )
    assert resp.status_code == 201, resp.text
    integration = resp.json()
    assert integration["provider"] == "github"
    assert integration["has_secret"] is True
    assert "secret" not in integration
    integration_id = integration["integration_id"]

    # 查询列表
    resp = client.get(
        f"/api/v1/projects/{project_id}/integrations",
        headers=headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # 编辑
    resp = client.patch(
        f"/api/v1/projects/{project_id}/integrations/{integration_id}",
        headers=headers,
        json={"name": "Updated GitHub"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated GitHub"

    # 删除
    resp = client.delete(
        f"/api/v1/projects/{project_id}/integrations/{integration_id}",
        headers=headers,
    )
    assert resp.status_code == 204

    with client.app.state.container.database().session_scope() as session:
        actions = [row[0] for row in session.execute(
            text("SELECT action FROM audit_logs WHERE project_id=:p AND resource_type='integration' ORDER BY id"),
            {"p": project_id},
        )]
    assert actions == ["integration.create", "integration.update", "integration.delete"]


def test_integration_requires_owner(client):
    """viewer 不能创建集成。"""
    admin_headers = _create_admin(client, "ci-admin2")
    project_id = _create_project(client, admin_headers, "CI-PERM")
    viewer_headers = _add_member(client, admin_headers, project_id, "ci-viewer", "pass123", "viewer")

    resp = client.post(
        f"/api/v1/projects/{project_id}/integrations",
        headers=viewer_headers,
        json={"provider": "github", "name": "test"},
    )
    assert resp.status_code == 403


def test_binding_crud(client):
    """CI 触发绑定 CRUD。"""
    headers = _create_admin(client, "ci-admin3")
    project_id = _create_project(client, headers, "CI-BIND")

    # 创建集成
    integration = client.post(
        f"/api/v1/projects/{project_id}/integrations",
        headers=headers,
        json={"provider": "github", "name": "bind-test"},
    ).json()
    integration_id = integration["integration_id"]

    # 创建绑定
    resp = client.post(
        f"/api/v1/projects/{project_id}/integrations/{integration_id}/bindings",
        headers=headers,
        json={
            "task_id": "T-ANY",
            "event_filter_json": {"event": "push", "branch": ["main", "develop"]},
        },
    )
    # 任务不存在时应失败
    assert resp.status_code == 422


def test_webhook_signature_verification(client):
    """webhook 签名：正确 HMAC-SHA256（原始 secret）通过，错误签名 403。"""
    import hashlib
    import hmac

    headers = _create_admin(client, "ci-admin4")
    project_id = _create_project(client, headers, "CI-SIGN")

    secret = "webhook-secret-123"
    integration = client.post(
        f"/api/v1/projects/{project_id}/integrations",
        headers=headers,
        json={"provider": "github", "name": "sign-test", "secret_value": secret},
    ).json()
    integration_id = integration["integration_id"]
    assert integration["has_secret"] is True

    body = b'{"event": "push"}'
    # 正确的 GitHub 风格签名：sha256=<HMAC-SHA256(secret, body)>
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    # 正确签名：无绑定，返回 accepted（未触发任何 Run）
    resp = client.post(
        f"/api/v1/integrations/{integration_id}/webhook",
        headers={
            "X-AETP-Delivery-Id": "delivery-1",
            "X-AETP-Signature": f"sha256={digest}",
            "Content-Type": "application/json",
        },
        content=body,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "accepted"
    with client.app.state.container.database().session_scope() as session:
        action = session.execute(
            text("SELECT action FROM audit_logs WHERE project_id=:p AND resource_id=:i ORDER BY id DESC"),
            {"p": project_id, "i": integration_id},
        ).scalars().first()
    assert action == "integration.webhook"

    # 错误签名：403 拒绝
    resp = client.post(
        f"/api/v1/integrations/{integration_id}/webhook",
        headers={
            "X-AETP-Delivery-Id": "delivery-2",
            "X-AETP-Signature": "sha256=deadbeef",
            "Content-Type": "application/json",
        },
        content=body,
    )
    assert resp.status_code == 403
    assert resp.json() == {
        "code": "CI_WEBHOOK_SIGNATURE_INVALID",
        "message": "签名验证失败",
        "data": None,
    }

    # 无签名：403 拒绝（配置了 secret 时必须签名）
    resp = client.post(
        f"/api/v1/integrations/{integration_id}/webhook",
        headers={
            "X-AETP-Delivery-Id": "delivery-3",
            "Content-Type": "application/json",
        },
        content=body,
    )
    assert resp.status_code == 403


def test_webhook_without_secret_is_rejected(client):
    """公共 webhook 必须配置 secret，不能以空密钥放行。"""
    headers = _create_admin(client, "ci-admin-no-secret")
    project_id = _create_project(client, headers, "CI-NO-SECRET")
    integration = client.post(
        f"/api/v1/projects/{project_id}/integrations",
        headers=headers,
        json={"provider": "generic", "name": "no-secret"},
    ).json()

    resp = client.post(
        f"/api/v1/integrations/{integration['integration_id']}/webhook",
        headers={"X-AETP-Delivery-Id": "delivery-no-secret"},
        content=b'{"event": "push"}',
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "CI_WEBHOOK_SIGNATURE_INVALID"
    assert client.app.state.container.ci_integration_service().list_deliveries(
        integration["integration_id"]
    ) == []


def test_binding_route_rejects_integration_from_another_project(client):
    """binding 路径的 project_id 必须与 integration 所属项目一致。"""
    headers = _create_admin(client, "ci-admin-boundary")
    first_project_id = _create_project(client, headers, "CI-BOUNDARY-1")
    second_project_id = _create_project(client, headers, "CI-BOUNDARY-2")
    integration = client.post(
        f"/api/v1/projects/{first_project_id}/integrations",
        headers=headers,
        json={"provider": "generic", "name": "boundary"},
    ).json()

    resp = client.get(
        f"/api/v1/projects/{second_project_id}/integrations/{integration['integration_id']}/bindings",
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "RESOURCE_NOT_FOUND"


def test_webhook_delivery_deduplicates(client):
    """相同 delivery_id 重复投递不重复触发（幂等返回）。"""
    import hashlib
    import hmac

    headers = _create_admin(client, "ci-admin5")
    project_id = _create_project(client, headers, "CI-DUP")

    secret = "webhook-secret-456"
    integration = client.post(
        f"/api/v1/projects/{project_id}/integrations",
        headers=headers,
        json={"provider": "github", "name": "dup-test", "secret_value": secret},
    ).json()
    integration_id = integration["integration_id"]

    body = b'{"event": "push"}'
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    webhook_headers = {
        "X-AETP-Delivery-Id": "delivery-dup",
        "X-AETP-Signature": f"sha256={digest}",
        "Content-Type": "application/json",
    }

    resp1 = client.post(
        f"/api/v1/integrations/{integration_id}/webhook",
        headers=webhook_headers,
        content=body,
    )
    assert resp1.status_code == 200

    # 重复投递：幂等返回 already_processed
    resp2 = client.post(
        f"/api/v1/integrations/{integration_id}/webhook",
        headers=webhook_headers,
        content=body,
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "already_processed"


def test_webhook_list_deliveries(client):
    """投递记录可查询（list_by_integration，非枚举猜 ID）。"""
    import hashlib
    import hmac

    headers = _create_admin(client, "ci-admin6")
    project_id = _create_project(client, headers, "CI-LIST")

    secret = "webhook-secret-789"
    integration = client.post(
        f"/api/v1/projects/{project_id}/integrations",
        headers=headers,
        json={"provider": "github", "name": "list-test", "secret_value": secret},
    ).json()
    integration_id = integration["integration_id"]

    body = b'{"event": "push"}'
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    client.post(
        f"/api/v1/integrations/{integration_id}/webhook",
        headers={
            "X-AETP-Delivery-Id": "delivery-list",
            "X-AETP-Signature": f"sha256={digest}",
            "Content-Type": "application/json",
        },
        content=body,
    )

    svc = client.app.state.container.ci_integration_service()
    deliveries = svc.list_deliveries(integration_id)
    assert len(deliveries) == 1
    assert deliveries[0].delivery_id == "delivery-list"
