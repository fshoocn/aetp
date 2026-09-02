"""M7：V2-only 空库上的 API 写读闭环。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from aetp_protocol.ids import BusinessId
from fastapi import FastAPI
from fastapi.testclient import TestClient

import master.main as main_module
from master import config
from tests.test_v2_task_service import SCRIPT_A, _binding, _definition


def test_v2_only_api_creates_and_reads_v2_run(tmp_path: Path) -> None:
    env_file = tmp_path / "master-v2.env"
    env_file.write_text(
        "AETP_MASTER_PROFILE=v2\n"
        f"AETP_MASTER_DATABASE_URL=sqlite:///{(tmp_path / 'v2.db').as_posix()}\n"
        f"AETP_MASTER_DATA_DIR={(tmp_path / 'data').as_posix()}\n"
        "AETP_MASTER_JWT_SECRET=test-secret-at-least-32-bytes-long-for-v2\n",
        encoding="utf-8",
    )
    config.reset_settings()
    config.configure(env_file)
    try:
        with TestClient(main_module.app) as client:
            container = cast(FastAPI, client.app).state.container
            auth = container.auth_service()
            auth.bootstrap_admin("m7-only-admin", "admin-pass-123", "M7 Only Admin")
            login = client.post(
                "/api/v2/auth/login",
                json={"username": "m7-only-admin", "password": "admin-pass-123"},
            )
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            project = client.post(
                "/api/v2/projects",
                headers=headers,
                json={"project_key": "M7ONLY", "name": "V2 Only"},
            )
            assert project.status_code == 201, project.text
            project_id = project.json()["project_id"]

            definition = _definition(SCRIPT_A, name="v2-only-script").model_copy(
                update={"project_id": BusinessId(project_id)}
            )
            created_definition = client.post(
                f"/api/v2/projects/{project_id}/script-definitions",
                headers=headers,
                json={"definition": definition.model_dump(mode="json")},
            )
            assert created_definition.status_code == 201, created_definition.text

            from aetp_protocol.task import TestTask

            task = TestTask(
                task_id=BusinessId("01J00000000000000000000049"),
                project_id=BusinessId(project_id),
                revision=1,
                name="v2-only-task",
                scripts=(_binding("01J0000000000000000000004A", definition, 0),),
            )
            created_task = client.post(
                f"/api/v2/projects/{project_id}/test-tasks",
                headers=headers,
                json={"task": task.model_dump(mode="json")},
            )
            assert created_task.status_code == 201, created_task.text

            created_run = client.post(
                f"/api/v2/projects/{project_id}/runs",
                headers=headers,
                json={"task_id": task.task_id.root},
            )
            assert created_run.status_code == 201, created_run.text
            run_id = created_run.json()["run_id"]
            detail = client.get(
                f"/api/v2/projects/{project_id}/runs/{run_id}",
                headers=headers,
            )
            assert detail.status_code == 200, detail.text
            assert detail.json()["task_id"] == task.task_id.root
            assert len(detail.json()["shards"]) == 2
    finally:
        config.reset_settings()
