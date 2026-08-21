from __future__ import annotations

import io
import json
import zipfile

import pytest
from aetp_protocol.capabilities import HardwareRequirements
from aetp_protocol.envelope import Envelope
from aetp_protocol.payloads import ScriptVerifyResultPayload
from aetp_protocol.plugin import PluginMetadata, PluginPackage

from master.application.services.script_download_service import ScriptDownloadService
from master.application.services.script_verification_service import (
    ScriptVerificationService,
)
from master.domain.enums import (
    AccountStatus,
    NodeStatus,
    PlatformRole,
    ProjectStatus,
    ScriptParseLocation,
    ScriptParseStatus,
)
from master.domain.models import (
    Node,
    Project,
    ProjectNodeBinding,
    TestScript,
    User,
)
from master.domain.time import utcnow
from master.plugins.manager import PluginManager


def _uow(container):
    return container.uow_factory()()


def _build_ui_plugin_zip() -> bytes:
    main = (
        "from aetp_protocol.plugin import PluginMetadata, PluginPackage\n"
        "from aetp_protocol.capabilities import HardwareRequirements\n"
        "class Master:\n"
        "    task_type='ui_task'; plugin_version='1.0.0'; supported_versions=frozenset({'1.0.0'})\n"
        "    config_schema={'type':'object'}; upload_spec={}\n"
        "    def verify_script(self, d, c): return []\n"
        "    async def parse_cases(self, d, c): return []\n"
        "    async def split_shards(self, c, p, cfg): return []\n"
        "    def build_task_definition(self, c, cs): return None\n"
        "    def result_schema(self, c): return {}\n"
        "    def hardware_requirements(self, c, cs): return HardwareRequirements()\n"
        "class Agent: pass\n"
        "package=PluginPackage(\n"
        "  metadata=PluginMetadata(task_type='ui_task', plugin_version='1.0.0',"
        " supported_versions=frozenset({'1.0.0'}),"
        " ui={'entry':'index.html','protocol_version':1}),"
        " master=Master(), agent=Agent())\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", json.dumps({"task_type": "ui_task", "plugin_version": "1.0.0"}))
        archive.writestr("main.py", main)
        archive.writestr("ui/index.html", "<h1>plugin ui</h1>")
    return buffer.getvalue()


def test_plugin_manager_loads_ui_asset_from_installed_zip(tmp_path) -> None:
    manager = PluginManager(tmp_path)
    record = manager.upload("ui_task.zip", _build_ui_plugin_zip())
    manager.install(record.plugin_id)

    package = manager.load_packages()[0]
    asset = manager.ui_asset("ui_task", "index.html")

    assert package.metadata.ui["entry"] == "index.html"
    assert asset.read_text(encoding="utf-8") == "<h1>plugin ui</h1>"


def test_task_type_context_exposes_plugin_upload_spec(client) -> None:
    registry = client.app.state.container.plugin_registry()
    auth_service = client.app.state.container.auth_service()
    assert auth_service.bootstrap_admin("context-admin", "admin-pass-123", "Context Admin")
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "context-admin", "password": "admin-pass-123"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    class Master:
        task_type = "context_task"
        plugin_version = "1.0.0"
        supported_versions = frozenset({"1.0.0"})

    class Agent:
        task_type = "context_task"
        plugin_version = "1.0.0"
        supported_versions = frozenset({"1.0.0"})

    registry.register(
        PluginPackage(
            metadata=PluginMetadata(
                task_type="context_task",
                plugin_version="1.0.0",
                supported_versions=frozenset({"1.0.0"}),
                upload_spec={"extensions": [".cdd"], "max_size_mb": 12},
                ui={"entry": "index.html", "protocol_version": 1},
            ),
            master=Master(),
            agent=Agent(),
        )
    )
    project = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"project_key": "CONTEXT", "name": "Context"},
    )
    assert project.status_code == 201

    response = client.get(
        f"/api/v1/projects/{project.json()['project_id']}/task-types/context_task/config-context",
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["upload_spec"] == {"extensions": [".cdd"], "max_size_mb": 12}


def test_script_verification_dispatches_and_persists_result(client) -> None:
    container = client.app.state.container
    registry = container.plugin_registry()

    class Master:
        task_type = "verify_task"
        plugin_version = "1.0.0"
        supported_versions = frozenset({"1.0.0"})
        config_schema = {"type": "object"}  # noqa: RUF012
        upload_spec = {"extensions": [".py"]}  # noqa: RUF012

        def verify_script(self, _directory, _config):
            return []

    class Agent:
        task_type = "verify_task"
        plugin_version = "1.0.0"
        supported_versions = frozenset({"1.0.0"})
        verify_location = "agent"
        parse_location = "master"

    registry.register(
        PluginPackage(
            metadata=PluginMetadata(
                task_type="verify_task",
                plugin_version="1.0.0",
                supported_versions=frozenset({"1.0.0"}),
            ),
            master=Master(),
            agent=Agent(),
        )
    )

    with _uow(container) as uow:
        user = uow.users.add(
            User(
                id=None,
                username="verify-owner",
                password_hash="h",
                display_name="",
                account_status=AccountStatus.ACTIVE,
                platform_role=PlatformRole.USER,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        uow.projects.add(
            Project(
                id=None,
                project_id="p-verify",
                project_key="P-VERIFY",
                name="Verify",
                description="",
                status=ProjectStatus.ACTIVE,
                created_by=user.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        uow.test_scripts.add(
            TestScript(
                project_id="p-verify",
                script_id="S-verify",
                task_type="verify_task",
                name="verify",
                version=1,
                file_ref="scripts/S-verify/1.py",
                size=1,
                sha256="a" * 64,
                config={},
                hardware_requirements=HardwareRequirements(),
                parse_status=ScriptParseStatus.PARSED,
                parse_location=ScriptParseLocation.MASTER,
                result_parse_location=ScriptParseLocation.MASTER,
                plugin_version="1.0.0",
                created_by=user.id,
            )
        )
        uow.nodes.save(
            Node(
                id=None,
                node_id="node-verify",
                name="Verify Node",
                hostname="verify-node",
                status=NodeStatus.ONLINE,
                online=True,
                enabled=True,
                last_seen_at=utcnow(),
            )
        )
        uow.bindings.add(
            ProjectNodeBinding(
                id=None,
                project_id="p-verify",
                node_id="node-verify",
                enabled=True,
                assigned_by=user.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )

    service = ScriptVerificationService(
        container.uow_factory(),
        registry,
        ScriptDownloadService("secret", base_url="http://master.local"),
    )
    dispatched = service.request(
        project_id="p-verify",
        script_id="S-verify",
        node_id="node-verify",
        config={"strict": True},
    )
    assert dispatched["status"] == "dispatched"

    with _uow(container) as uow:
        outbox = uow.outbox_messages.get_by_outbox_id(f"script-verify:{dispatched['verify_id']}")
        assert outbox is not None
        envelope = Envelope.model_validate(outbox.payload)
        assert envelope.payload["project_id"] == "p-verify"
        assert envelope.payload["script_ref"]["download_url"].startswith("http://master.local")

    result = service.handle_result(
        "node-verify",
        ScriptVerifyResultPayload(
            verify_id=dispatched["verify_id"],
            script_id="S-verify",
            project_id="p-verify",
            errors=["COM unavailable"],
        ),
    )
    assert result is not None
    assert result.errors == ["COM unavailable"]
    stored = service.get_result("p-verify", dispatched["verify_id"])
    assert stored is not None
    assert stored["errors"] == ["COM unavailable"]

    with pytest.raises(ValueError, match="项目"):
        service.handle_result(
            "node-verify",
            ScriptVerifyResultPayload(
                verify_id=dispatched["verify_id"],
                script_id="S-verify",
                project_id="other-project",
                errors=[],
            ),
        )

    assert (
        service.handle_result(
            "node-verify",
            ScriptVerifyResultPayload(
                verify_id=dispatched["verify_id"],
                script_id="S-verify",
                project_id="p-verify",
                errors=["duplicate"],
            ),
        )
        is None
    )
    with _uow(container) as uow:
        events = uow.domain_events.list(project_id="p-verify", limit=100)
        assert [event.event_type for event in events].count("script.verify_result") == 1
