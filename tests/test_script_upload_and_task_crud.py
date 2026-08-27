"""P7.3/P7.4：脚本上传 + 任务定义 CRUD API 测试（§18.3/§18.4）。"""

from __future__ import annotations

import json

import pytest
from aetp_protocol.capabilities import HardwareRequirements
from aetp_protocol.plugin import CaseInfo, PluginMetadata, PluginPackage, ShardSpec
from sqlalchemy import text

from master.adapters.sqlalchemy.orm import Node as NodeORM
from master.application.services.script_service import ScriptService, ScriptUploadError
from master.domain.enums import (
    NodeStatus,
)


def _uow(container):
    return container.uow_factory()()


class _UploadPlugin:
    """测试插件：verify 通过，parse 产出 3 个用例。"""

    task_type = "upload_test"
    display_name = "Upload Test"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    config_schema = {"type": "object"}  # noqa: RUF012
    upload_spec = {"extensions": [".py", ".zip"], "max_size_mb": 10}  # noqa: RUF012

    def verify_script(self, script_dir, config):
        return [] if not config.get("broken") else ["配置了 broken=true"]

    async def parse_cases(self, script_dir, config):
        return [
            CaseInfo(stable_key="case-a", name="Case A", estimated_duration_s=5),
            CaseInfo(stable_key="case-b", name="Case B", estimated_duration_s=8),
            CaseInfo(stable_key="case-c", name="Case C", estimated_duration_s=4),
        ]

    async def split_shards(self, cases, policy, config):
        return [ShardSpec(case_keys=tuple(c.stable_key for c in cases))]

    def build_task_definition(self, config, cases):
        return object()

    def result_schema(self, config):
        return {"type": "object"}

    def hardware_requirements(self, config, cases):
        required_tag = config.get("required_tag")
        return HardwareRequirements(required_tags=(required_tag,) if required_tag else ())


def _register_plugin(container) -> None:
    registry = container.plugin_registry()
    if registry.get("upload_test") is not None:
        return
    registry.register(
        PluginPackage(
            metadata=PluginMetadata(
                task_type="upload_test",
                plugin_version="1.0.0",
                supported_versions=frozenset({"1.0.0"}),
            ),
            master=_UploadPlugin(),
            agent=object(),
        )
    )


def _create_admin(client):
    service = client.app.state.container.auth_service()
    assert service.bootstrap_admin("script-admin", "admin-pass-123", "SA")
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "script-admin", "password": "admin-pass-123"},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_project(client, headers, key="SCRIPTS"):
    resp = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"project_key": key, "name": key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project_id"]


def _create_node(client, node_id="bench-001"):
    with client.app.state.container.database().session_scope() as session:
        session.add(
            NodeORM(
                node_id=node_id,
                name=node_id,
                hostname=node_id,
                status=NodeStatus.ONLINE.value,
                online=True,
                enabled=True,
            )
        )


def _bind_node(client, headers, project_id, node_id="bench-001"):
    resp = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        headers=headers,
        json={"node_id": node_id},
    )
    assert resp.status_code == 201, resp.text


def _add_member(client, admin_headers, project_id, username, role):
    service = client.app.state.container.auth_service()
    user = service.create_user(username, "member-pass-123", username)
    with client.app.state.container.database().session_scope() as session:
        session.execute(
            text("UPDATE users SET account_status='active' WHERE username=:username"),
            {"username": username},
        )
    login = client.post("/api/v1/auth/login", json={"username": username, "password": "member-pass-123"})
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
        json={"user_id": user.id, "project_role": role},
    )
    assert response.status_code == 201, response.text
    return member_headers


def test_upload_spec_rejects_malformed_size_and_extensions() -> None:
    """upload_spec 形状异常时统一返回脚本上传错误。"""
    with pytest.raises(ScriptUploadError, match="extensions"):
        ScriptService._validate_upload_spec({"extensions": ".py"}, "test.py", b"x")
    with pytest.raises(ScriptUploadError, match="扩展名数组"):
        ScriptService._validate_upload_spec({"extensions": ["py"]}, "test.py", b"x")
    with pytest.raises(ScriptUploadError, match="扩展名数组"):
        ScriptService._validate_upload_spec({"extensions": [".py", 1]}, "test.py", b"x")
    with pytest.raises(ScriptUploadError, match="max_size_mb"):
        ScriptService._validate_upload_spec({"max_size_mb": 0}, "test.py", b"x")
    with pytest.raises(ScriptUploadError, match="max_size_mb"):
        ScriptService._validate_upload_spec({"max_size_mb": True}, "test.py", b"x")


def test_script_upload_parse_and_case_list(client):
    """脚本上传 → 验证 → 解析 → 用例列表全流程。"""
    container = client.app.state.container
    _register_plugin(container)
    headers = _create_admin(client)
    project_id = _create_project(client, headers)

    # 上传脚本（.py 文件）
    resp = client.post(
        f"/api/v1/projects/{project_id}/scripts",
        headers=headers,
        data={"task_type": "upload_test", "name": "reg", "config": "{}"},
        files={"file": ("test_sample.py", b"def test_a():\n    pass\n", "text/x-python")},
    )
    assert resp.status_code == 201, resp.text
    script = resp.json()
    assert script["task_type"] == "upload_test"
    assert script["name"] == "reg"
    assert script["version"] == 1
    assert script["parse_status"] == "parsed"
    assert script["plugin_version"] == "1.0.0"

    # 用例列表
    resp = client.get(
        f"/api/v1/projects/{project_id}/scripts/{script['script_id']}/cases",
        headers=headers,
    )
    assert resp.status_code == 200
    cases = resp.json()
    assert [c["stable_key"] for c in cases] == ["case-a", "case-b", "case-c"]
    assert cases[0]["avg_duration_s"] == 5

    # 脚本列表/详情
    resp = client.get(f"/api/v1/projects/{project_id}/scripts", headers=headers)
    assert resp.status_code == 200
    assert any(s["script_id"] == script["script_id"] for s in resp.json())
    resp = client.get(
        f"/api/v1/projects/{project_id}/scripts/{script['script_id']}",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "reg"


def test_script_upload_verify_failure(client):
    """插件 verify_script 返回错误 → 422 且不写库。"""
    container = client.app.state.container
    _register_plugin(container)
    headers = _create_admin(client)
    project_id = _create_project(client, headers, key="SCRIPT2")

    resp = client.post(
        f"/api/v1/projects/{project_id}/scripts",
        headers=headers,
        data={"task_type": "upload_test", "name": "bad", "config": json.dumps({"broken": True})},
        files={"file": ("bad.py", b"x = 1\n", "text/x-python")},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "SCRIPT_PARSE_FAILED"
    assert "broken" in resp.json()["message"]


def test_script_upload_rejects_extension(client):
    """扩展名不在 upload_spec 白名单 → 422。"""
    container = client.app.state.container
    _register_plugin(container)
    headers = _create_admin(client)
    project_id = _create_project(client, headers, key="SCRIPT3")

    resp = client.post(
        f"/api/v1/projects/{project_id}/scripts",
        headers=headers,
        data={"task_type": "upload_test", "name": "bad", "config": "{}"},
        files={"file": ("bad.exe", b"not-a-script", "application/octet-stream")},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"
    assert "不支持的文件类型" in resp.json()["message"]


def test_script_upload_rejects_unknown_task_type(client):
    headers = _create_admin(client)
    project_id = _create_project(client, headers, key="SCRIPT_UNKNOWN_PLUGIN")

    resp = client.post(
        f"/api/v1/projects/{project_id}/scripts",
        headers=headers,
        data={"task_type": "missing_plugin", "name": "bad", "config": "{}"},
        files={"file": ("bad.py", b"x = 1\n", "text/x-python")},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "PLUGIN_NOT_FOUND"


def test_script_upload_same_hash_idempotent(client):
    """同 sha256 重复上传幂等复用（不产生新版本）。"""
    container = client.app.state.container
    _register_plugin(container)
    headers = _create_admin(client)
    project_id = _create_project(client, headers, key="SCRIPT4")

    content = b"def test_x():\n    pass\n"
    data = {"task_type": "upload_test", "name": "dup", "config": "{}"}
    files = {"file": ("dup.py", content, "text/x-python")}

    resp1 = client.post(f"/api/v1/projects/{project_id}/scripts", headers=headers, data=data, files=files)
    assert resp1.status_code == 201
    resp2 = client.post(f"/api/v1/projects/{project_id}/scripts", headers=headers, data=data, files=files)
    assert resp2.status_code == 201
    assert resp1.json()["script_id"] == resp2.json()["script_id"]
    assert resp1.json()["version"] == resp2.json()["version"] == 1


def test_script_upload_same_hash_isolated_between_projects(client):
    """相同内容只在同一项目内幂等，不能跨项目复用脚本记录。"""
    container = client.app.state.container
    _register_plugin(container)
    headers = _create_admin(client)
    project_a = _create_project(client, headers, key="SCRIPT_HASH_A")
    project_b = _create_project(client, headers, key="SCRIPT_HASH_B")
    content = b"def test_shared_content():\n    pass\n"

    def upload(project_id: str, name: str):
        return client.post(
            f"/api/v1/projects/{project_id}/scripts",
            headers=headers,
            data={"task_type": "upload_test", "name": name, "config": "{}"},
            files={"file": ("shared.py", content, "text/x-python")},
        )

    first = upload(project_a, "shared-a")
    second = upload(project_b, "shared-b")
    assert first.status_code == second.status_code == 201
    assert first.json()["script_id"] != second.json()["script_id"]
    assert first.json()["project_id"] == project_a
    assert second.json()["project_id"] == project_b


def test_script_delete_removes_script_cases_and_file(client):
    """删除脚本版本时同时清理用例索引和存储文件。"""
    container = client.app.state.container
    _register_plugin(container)
    headers = _create_admin(client)
    project_id = _create_project(client, headers, key="SCRIPT_DELETE")

    resp = client.post(
        f"/api/v1/projects/{project_id}/scripts",
        headers=headers,
        data={"task_type": "upload_test", "name": "delete-me", "config": "{}"},
        files={"file": ("delete.py", b"def test_delete():\n    pass\n", "text/x-python")},
    )
    assert resp.status_code == 201, resp.text
    script = resp.json()
    file_ref = script["file_ref"]
    assert container.storage().exists(file_ref)

    deleted = client.delete(
        f"/api/v1/projects/{project_id}/scripts/{script['script_id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text
    assert not container.storage().exists(file_ref)
    missing_script = client.get(
        f"/api/v1/projects/{project_id}/scripts/{script['script_id']}",
        headers=headers,
    )
    assert missing_script.status_code == 404
    assert missing_script.json()["code"] == "SCRIPT_NOT_FOUND"
    missing_cases = client.get(
        f"/api/v1/projects/{project_id}/scripts/{script['script_id']}/cases",
        headers=headers,
    )
    assert missing_cases.status_code == 404
    assert missing_cases.json()["code"] == "SCRIPT_NOT_FOUND"


def test_script_delete_rejects_task_definition_reference(client):
    """脚本仍被启用中的任务定义引用时，删除返回 409。"""
    container = client.app.state.container
    _register_plugin(container)
    headers = _create_admin(client)
    project_id = _create_project(client, headers, key="SCRIPT_DELETE_REF")

    resp = client.post(
        f"/api/v1/projects/{project_id}/scripts",
        headers=headers,
        data={"task_type": "upload_test", "name": "referenced", "config": "{}"},
        files={"file": ("referenced.py", b"def test_ref():\n    pass\n", "text/x-python")},
    )
    assert resp.status_code == 201
    script_id = resp.json()["script_id"]
    task = client.post(
        f"/api/v1/projects/{project_id}/test-tasks",
        headers=headers,
        json={"name": "keeps-script", "script_id": script_id},
    )
    assert task.status_code == 201, task.text

    deleted = client.delete(
        f"/api/v1/projects/{project_id}/scripts/{script_id}",
        headers=headers,
    )
    assert deleted.status_code == 409
    assert deleted.json()["code"] == "CONFLICT"
    assert "任务定义引用" in deleted.json()["message"]


def test_script_deletable_after_task_deleted(client):
    """删除无 Run 历史的任务定义后，脚本可以正常删除。"""
    container = client.app.state.container
    _register_plugin(container)
    headers = _create_admin(client)
    project_id = _create_project(client, headers, key="SCRIPT_DEL_HARD")

    resp = client.post(
        f"/api/v1/projects/{project_id}/scripts",
        headers=headers,
        data={"task_type": "upload_test", "name": "to-delete", "config": "{}"},
        files={"file": ("to_delete.py", b"def test_d():\n    pass\n", "text/x-python")},
    )
    assert resp.status_code == 201
    script_id = resp.json()["script_id"]
    task = client.post(
        f"/api/v1/projects/{project_id}/test-tasks",
        headers=headers,
        json={"name": "will-hard-delete", "script_id": script_id},
    )
    assert task.status_code == 201
    task_id = task.json()["task_id"]

    # 删除无 Run 历史的任务 → 硬删除
    r1 = client.delete(f"/api/v1/projects/{project_id}/test-tasks/{task_id}", headers=headers)
    assert r1.status_code == 204

    # 脚本现在可以删除（无引用）
    r3 = client.delete(f"/api/v1/projects/{project_id}/scripts/{script_id}", headers=headers)
    assert r3.status_code == 204


def test_task_deletable_with_run_history(client):
    """有 Run 历史的任务定义也可删除，历史 Run 保留（task 引用置空）。"""
    from master.domain.models import TaskRun

    container = client.app.state.container
    _register_plugin(container)
    headers = _create_admin(client)
    project_id = _create_project(client, headers, key="TASK_DEL_WITH_RUNS")

    resp = client.post(
        f"/api/v1/projects/{project_id}/scripts",
        headers=headers,
        data={"task_type": "upload_test", "name": "with-runs", "config": "{}"},
        files={"file": ("with_runs.py", b"def test_w():\n    pass\n", "text/x-python")},
    )
    assert resp.status_code == 201
    script_id = resp.json()["script_id"]
    task = client.post(
        f"/api/v1/projects/{project_id}/test-tasks",
        headers=headers,
        json={"name": "has-runs", "script_id": script_id},
    )
    assert task.status_code == 201
    task_id = task.json()["task_id"]

    # 直接造一个历史 Run
    with _uow(container) as uow:
        run = uow.task_runs.add(
            TaskRun(
                run_id="R-DEL-TEST",
                project_id=project_id,
                task_id=task_id,
                script_ref={"script_id": script_id, "version": 1, "sha256": "x"},
            )
        )
        assert run.id is not None

    # 删除任务 → 硬删除，历史 Run 保留
    r = client.delete(f"/api/v1/projects/{project_id}/test-tasks/{task_id}", headers=headers)
    assert r.status_code == 204

    # Run 仍存在，但 task 引用已置空
    with _uow(container) as uow:
        run = uow.task_runs.get_by_run_id("R-DEL-TEST")
        assert run is not None
        assert run.task_id == ""


def test_operator_can_create_test_task_definition(client):
    """operator 可以创建任务定义，但不能上传脚本。"""
    container = client.app.state.container
    _register_plugin(container)
    admin_headers = _create_admin(client)
    project_id = _create_project(client, admin_headers, key="TASK_OPERATOR")
    _create_node(client)
    _bind_node(client, admin_headers, project_id)
    script = client.post(
        f"/api/v1/projects/{project_id}/scripts",
        headers=admin_headers,
        data={"task_type": "upload_test", "name": "operator-source", "config": "{}"},
        files={"file": ("test_sample.py", b"def test_a():\n    pass\n", "text/x-python")},
    )
    assert script.status_code == 201, script.text
    operator_headers = _add_member(client, admin_headers, project_id, "task-operator", "operator")

    denied_upload = client.post(
        f"/api/v1/projects/{project_id}/scripts",
        headers=operator_headers,
        data={"task_type": "upload_test", "name": "denied", "config": "{}"},
        files={"file": ("test_sample.py", b"def test_a():\n    pass\n", "text/x-python")},
    )
    assert denied_upload.status_code == 403

    created = client.post(
        f"/api/v1/projects/{project_id}/test-tasks",
        headers=operator_headers,
        json={"name": "operator-task", "script_id": script.json()["script_id"]},
    )
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "operator-task"
    endpoint = client.post(
        f"/api/v1/projects/{project_id}/notification-endpoints",
        headers=admin_headers,
        json={"channel_type": "console_test", "name": "task-progress"},
    )
    assert endpoint.status_code == 201, endpoint.text
    subscription = client.post(
        f"/api/v1/projects/{project_id}/event-subscriptions",
        headers=operator_headers,
        json={
            "endpoint_id": endpoint.json()["endpoint_id"],
            "task_id": created.json()["task_id"],
            "event_types": ["run.progress", "run.result"],
        },
    )
    assert subscription.status_code == 201, subscription.text
    assert subscription.json()["task_id"] == created.json()["task_id"]


def test_task_definition_crud_and_case_selection(client):
    """任务定义 CRUD：创建（case 勾选校验）→ 查询 → 更新 → 删除。"""
    container = client.app.state.container
    _register_plugin(container)
    headers = _create_admin(client)
    project_id = _create_project(client, headers, key="SCRIPTS")
    _create_node(client)
    _bind_node(client, headers, project_id)

    # 先上传脚本
    resp = client.post(
        f"/api/v1/projects/{project_id}/scripts",
        headers=headers,
        data={"task_type": "upload_test", "name": "reg", "config": "{}"},
        files={"file": ("test_sample.py", b"def test_a():\n    pass\n", "text/x-python")},
    )
    assert resp.status_code == 201
    script_id = resp.json()["script_id"]

    # 创建任务定义
    resp = client.post(
        f"/api/v1/projects/{project_id}/test-tasks",
        headers=headers,
        json={
            "name": "reg-task",
            "script_id": script_id,
            "default_case_selection": ["case-a", "case-b"],
            "node_ids": ["bench-001"],
            "split_policy": {"type": "by_case_count", "cases_per_shard": 2},
            "retry_policy": {"max_attempts": 2},
            "config": {"pytest_args": ["-q"], "fail_fast": True},
            "timeout_s": 3600,
        },
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["name"] == "reg-task"
    assert task["script_id"] == script_id
    assert task["default_case_selection"] == ["case-a", "case-b"]
    assert task["node_ids"] == ["bench-001"]
    assert task["config"] == {"pytest_args": ["-q"], "fail_fast": True}
    assert task["enabled"] is True
    task_id = task["task_id"]

    # 查询列表/详情
    resp = client.get(f"/api/v1/projects/{project_id}/test-tasks", headers=headers)
    assert resp.status_code == 200
    assert any(t["task_id"] == task_id for t in resp.json())
    resp = client.get(f"/api/v1/projects/{project_id}/test-tasks/{task_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "reg-task"

    # 更新（切换勾选 + 分割策略）
    resp = client.patch(
        f"/api/v1/projects/{project_id}/test-tasks/{task_id}",
        headers=headers,
        json={
            "default_case_selection": ["case-c"],
            "split_policy": {"type": "by_time", "target_duration_s": 60},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["default_case_selection"] == ["case-c"]
    assert resp.json()["split_policy"]["type"] == "by_time"

    resp = client.patch(
        f"/api/v1/projects/{project_id}/test-tasks/{task_id}",
        headers=headers,
        json={"config": {"pytest_args": ["-x"]}},
    )
    assert resp.status_code == 200
    assert resp.json()["config"] == {"pytest_args": ["-x"]}

    # 编辑时同样校验用例和分割参数
    resp = client.patch(
        f"/api/v1/projects/{project_id}/test-tasks/{task_id}",
        headers=headers,
        json={"default_case_selection": ["not-exist"]},
    )
    assert resp.status_code == 422
    assert "用例不存在" in resp.json()["message"]
    resp = client.patch(
        f"/api/v1/projects/{project_id}/test-tasks/{task_id}",
        headers=headers,
        json={"split_policy": {"type": "by_case_count", "cases_per_shard": 0}},
    )
    assert resp.status_code == 422
    assert "cases_per_shard" in resp.json()["message"]

    # 非法 case 勾选 → 422
    resp = client.post(
        f"/api/v1/projects/{project_id}/test-tasks",
        headers=headers,
        json={
            "name": "bad-task",
            "script_id": script_id,
            "default_case_selection": ["not-exist"],
        },
    )
    assert resp.status_code == 422
    assert "用例不存在" in resp.json()["message"]

    # 非项目绑定节点 → 403
    resp = client.post(
        f"/api/v1/projects/{project_id}/test-tasks",
        headers=headers,
        json={
            "name": "bad-node-task",
            "script_id": script_id,
            "node_ids": ["bench-999"],
        },
    )
    assert resp.status_code == 403

    # 删除（无 Run 历史 → 硬删除）
    resp = client.delete(f"/api/v1/projects/{project_id}/test-tasks/{task_id}", headers=headers)
    assert resp.status_code == 204
    resp = client.get(f"/api/v1/projects/{project_id}/test-tasks", headers=headers)
    assert all(t["task_id"] != task_id for t in resp.json())

    # 再次删除 → 404（已不存在）
    resp = client.delete(f"/api/v1/projects/{project_id}/test-tasks/{task_id}", headers=headers)
    assert resp.status_code == 404


def test_task_definition_returns_node_capability_warning(client):
    """节点绑定合法但能力不满足时，保存成功并返回软校验告警。"""
    container = client.app.state.container
    _register_plugin(container)
    headers = _create_admin(client)
    project_id = _create_project(client, headers, key="TASK_WARNING")
    _create_node(client)
    _bind_node(client, headers, project_id)

    script = client.post(
        f"/api/v1/projects/{project_id}/scripts",
        headers=headers,
        data={
            "task_type": "upload_test",
            "name": "needs-tag",
            "config": json.dumps({"required_tag": "missing-tag"}),
        },
        files={"file": ("needs_tag.py", b"def test_tag():\n    pass\n", "text/x-python")},
    )
    assert script.status_code == 201

    task = client.post(
        f"/api/v1/projects/{project_id}/test-tasks",
        headers=headers,
        json={
            "name": "warning-task",
            "script_id": script.json()["script_id"],
            "default_case_selection": ["case-a"],
            "node_ids": ["bench-001"],
        },
    )
    assert task.status_code == 201, task.text
    assert "NODE_CAPABILITY_MISMATCH" in task.json()["validation_warning"]
