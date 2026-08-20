"""P7.3/P7.4：脚本上传 + 任务定义 CRUD API 测试（§18.3/§18.4）。"""

from __future__ import annotations

import json

from aetp_protocol.capabilities import HardwareRequirements
from aetp_protocol.plugin import CaseInfo, PluginMetadata, PluginPackage, ShardSpec

from master.adapters.sqlalchemy.orm import Node as NodeORM
from master.domain.enums import (
    AccountStatus,
    NodeStatus,
    PlatformRole,
    ProjectStatus,
    ScriptParseLocation,
    ScriptParseStatus,
)
from master.domain.models import (
    Project,
    ProjectNodeBinding,
    TestScript,
    User,
)
from master.domain.time import utcnow


def _uow(container):
    return container.uow_factory()()


class _UploadPlugin:
    """测试插件：verify 通过，parse 产出 3 个用例。"""

    task_type = "upload_test"
    display_name = "Upload Test"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    config_schema = {"type": "object"}
    upload_spec = {"extensions": [".py", ".zip"], "max_size_mb": 10}

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
        return HardwareRequirements(
            required_tags=(required_tag,) if required_tag else ()
        )


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
    resp = client.get(
        f"/api/v1/projects/{project_id}/scripts", headers=headers
    )
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
    assert "broken" in resp.json()["detail"]


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
    assert "不支持的文件类型" in resp.json()["detail"]


def test_script_upload_same_hash_idempotent(client):
    """同 sha256 重复上传幂等复用（不产生新版本）。"""
    container = client.app.state.container
    _register_plugin(container)
    headers = _create_admin(client)
    project_id = _create_project(client, headers, key="SCRIPT4")

    content = b"def test_x():\n    pass\n"
    data = {"task_type": "upload_test", "name": "dup", "config": "{}"}
    files = {"file": ("dup.py", content, "text/x-python")}

    resp1 = client.post(
        f"/api/v1/projects/{project_id}/scripts", headers=headers, data=data, files=files
    )
    assert resp1.status_code == 201
    resp2 = client.post(
        f"/api/v1/projects/{project_id}/scripts", headers=headers, data=data, files=files
    )
    assert resp2.status_code == 201
    assert resp1.json()["script_id"] == resp2.json()["script_id"]
    assert resp1.json()["version"] == resp2.json()["version"] == 1


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
    assert client.get(
        f"/api/v1/projects/{project_id}/scripts/{script['script_id']}",
        headers=headers,
    ).status_code == 404
    assert client.get(
        f"/api/v1/projects/{project_id}/scripts/{script['script_id']}/cases",
        headers=headers,
    ).status_code == 404


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
    assert "任务定义引用" in deleted.json()["detail"]


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
            "timeout_s": 3600,
        },
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["name"] == "reg-task"
    assert task["script_id"] == script_id
    assert task["default_case_selection"] == ["case-a", "case-b"]
    assert task["node_ids"] == ["bench-001"]
    assert task["enabled"] is True
    task_id = task["task_id"]

    # 查询列表/详情
    resp = client.get(
        f"/api/v1/projects/{project_id}/test-tasks", headers=headers
    )
    assert resp.status_code == 200
    assert any(t["task_id"] == task_id for t in resp.json())
    resp = client.get(
        f"/api/v1/projects/{project_id}/test-tasks/{task_id}", headers=headers
    )
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

    # 编辑时同样校验用例和分割参数
    resp = client.patch(
        f"/api/v1/projects/{project_id}/test-tasks/{task_id}",
        headers=headers,
        json={"default_case_selection": ["not-exist"]},
    )
    assert resp.status_code == 422
    assert "用例不存在" in resp.json()["detail"]
    resp = client.patch(
        f"/api/v1/projects/{project_id}/test-tasks/{task_id}",
        headers=headers,
        json={"split_policy": {"type": "by_case_count", "cases_per_shard": 0}},
    )
    assert resp.status_code == 422
    assert "cases_per_shard" in resp.json()["detail"]

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
    assert "用例不存在" in resp.json()["detail"]

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
    resp = client.delete(
        f"/api/v1/projects/{project_id}/test-tasks/{task_id}", headers=headers
    )
    assert resp.status_code == 204
    resp = client.get(
        f"/api/v1/projects/{project_id}/test-tasks", headers=headers
    )
    assert all(t["task_id"] != task_id for t in resp.json())

    # 再次删除 → 404（已不存在）
    resp = client.delete(
        f"/api/v1/projects/{project_id}/test-tasks/{task_id}", headers=headers
    )
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
