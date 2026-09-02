"""M7：V2 ScriptDefinition 上传、解析和签名下载闭环。"""

from __future__ import annotations

from tests.test_pytest_v2_executor import _plugin_archive, _script_archive


def test_v2_script_upload_parses_and_downloads_from_v2_storage(client) -> None:
    container = client.app.state.container
    plugin_data = _plugin_archive()
    governance = container.plugin_governance_service()
    record = governance.register_archive("pytest-v2.zip", plugin_data)
    record = governance.install(record.plugin_id, record.version)
    governance.request_enabled(record.plugin_id, record.version)
    record = governance.complete_restart(record.plugin_id, record.version, enabled=True)
    container.v2_plugin_registry().register(record)

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
    project_id = project.json()["project_id"]

    script_data = _script_archive()
    response = client.post(
        f"/api/v2/projects/{project_id}/script-definitions/upload",
        headers=headers,
        data={
            "name": "pytest V2 script",
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

    signed_url = container.v2_script_download_service().build_download_url(
        definition["script_definition_id"]
    )
    downloaded = client.get(signed_url)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == script_data
    assert downloaded.headers["x-checksum-sha256"] == definition["source"]["sha256"]
