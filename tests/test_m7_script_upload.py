"""M7： ScriptDefinition 上传、解析和签名下载闭环。"""

from __future__ import annotations

import json

from tests.test_pytest_executor import _plugin_archive, _script_archive


def _setup(client):
    """安装并启用 pytest executor，建管理员 + 项目，返回 (container, headers, project_id)。"""
    container = client.app.state.container
    plugin_data = _plugin_archive()
    governance = container.plugin_governance_service()
    record = governance.register_archive("pytest-v2.zip", plugin_data)
    record = governance.install(record.plugin_id, record.version)
    governance.request_enabled(record.plugin_id, record.version)
    record = governance.complete_restart(record.plugin_id, record.version, enabled=True)
    container.plugin_registry().register(record)

    auth = container.auth_service()
    auth.bootstrap_admin("m7-script-admin", "admin-pass-123", "M7 Script Admin")
    login = client.post(
        "/api/v2/auth/login",
        json={"username": "m7-script-admin", "password": "admin-pass-123"},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    project = client.post(
        "/api/v2/projects",
        headers=headers,
        json={"project_key": "M7SCRIPT", "name": "M7 Script"},
    )
    assert project.status_code == 201, project.text
    return container, headers, project.json()["project_id"]


def test_script_upload_parses_and_downloads_from_storage(client) -> None:
    container, headers, project_id = _setup(client)

    script_data = _script_archive()
    response = client.post(
        f"/api/v2/projects/{project_id}/script-definitions/upload",
        headers=headers,
        data={
            "name": "pytest  script",
            "executor_plugin_id": "org.pytest.executor",
            "executor_version": "2.0.0",
            "configuration": "{}",
        },
        files={"file": ("script.zip", script_data, "application/zip")},
    )
    assert response.status_code == 201, response.text
    definition = response.json()
    assert definition["executor"]["plugin_id"] == "org.pytest.executor"
    assert definition["source"]["download_url"] is None
    assert definition["cases"][0]["stable_key"] == "test_sample.py::test_sample"

    signed_url = container.script_download_service().build_download_url(
        definition["script_definition_id"]
    )
    downloaded = client.get(signed_url)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == script_data
    assert downloaded.headers["x-checksum-sha256"] == definition["source"]["sha256"]


def test_script_upload_accepts_pre_generated_cases(client) -> None:
    """插件 UI/后端已生成 cases 时，上传带 cases 直接落库，不再调 parse_cases。"""
    _container, headers, project_id = _setup(client)

    # 上传一个与内容无关的占位文件；cases 由调用方给出。
    placeholder = b"not-a-pytest-project"
    cases = [
        {"stable_key": "data.xlsx::row-1", "name": "row-1", "parent_path": "data.xlsx"},
        {"stable_key": "data.xlsx::row-2", "name": "row-2", "parent_path": "data.xlsx"},
    ]
    response = client.post(
        f"/api/v2/projects/{project_id}/script-definitions/upload",
        headers=headers,
        data={
            "name": "material-driven script",
            "executor_plugin_id": "org.pytest.executor",
            "executor_version": "2.0.0",
            "configuration": "{}",
            "cases": json.dumps(cases),
        },
        files={"file": ("data.xlsx", placeholder, "application/vnd.ms-excel")},
    )
    assert response.status_code == 201, response.text
    definition = response.json()
    assert definition["source"]["filename"] == "data.xlsx"
    assert [case["stable_key"] for case in definition["cases"]] == [
        "data.xlsx::row-1",
        "data.xlsx::row-2",
    ]


def test_script_upload_bare_py_preserves_filename_and_collects(client) -> None:
    """单 .py 上传保留原文件名，pytest 插件对显式文件路径收集用例。"""
    _container, headers, project_id = _setup(client)

    # 上传的裸 .py 文件名不含 test_ 前缀：parse_cases 应直接对该文件收集。
    bare_py = b"def test_boot():\n    assert True\n"
    response = client.post(
        f"/api/v2/projects/{project_id}/script-definitions/upload",
        headers=headers,
        data={
            "name": "bare py script",
            "executor_plugin_id": "org.pytest.executor",
            "executor_version": "2.0.0",
            "configuration": "{}",
        },
        files={"file": ("smoke.py", bare_py, "text/x-python")},
    )
    assert response.status_code == 201, response.text
    definition = response.json()
    assert definition["source"]["filename"] == "smoke.py"
    # stable_key 反映原文件名（pytest 直接 collect 该文件）
    assert definition["cases"][0]["stable_key"] == "smoke.py::test_boot"
