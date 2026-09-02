"""P4.7：脚本签名下载端点测试（§7.4/§18.8）。"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from datetime import UTC, datetime, timedelta

from aetp_protocol.capabilities import HardwareRequirements

from master.domain.enums import (
    AccountStatus,
    PlatformRole,
    ProjectStatus,
    ScriptParseLocation,
    ScriptParseStatus,
)
from master.domain.models import Project, TestScript, User
from master.domain.signed_url import (
    build_artifact_upload_path,
    build_signed_path,
    verify_artifact_upload_path,
    verify_signed_path,
)
from master.domain.time import utcnow


def _query(path: str) -> dict[str, str]:
    return {key: value[0] for key, value in urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).items()}


def test_signed_path_roundtrip() -> None:
    secret = "unit-test-secret"
    path = build_signed_path("S-1", secret, 300)
    qs = _query(path)
    assert verify_signed_path("S-1", int(qs["expires"]), qs["signature"], secret)


def test_signed_path_rejects_expired() -> None:
    secret = "unit-test-secret"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    path = build_signed_path("S-1", secret, 300, now=now)
    qs = _query(path)
    later = now + timedelta(seconds=301)
    assert not verify_signed_path("S-1", int(qs["expires"]), qs["signature"], secret, now=later)


def test_signed_path_rejects_wrong_script_id() -> None:
    secret = "unit-test-secret"
    path = build_signed_path("S-1", secret, 300)
    qs = _query(path)
    assert not verify_signed_path("S-2", int(qs["expires"]), qs["signature"], secret)


def test_signed_path_rejects_tampered_signature() -> None:
    secret = "unit-test-secret"
    path = build_signed_path("S-1", secret, 300)
    qs = _query(path)
    assert not verify_signed_path("S-1", int(qs["expires"]), "0" * 64, secret)


def test_artifact_upload_signature_binds_scope_and_expiry() -> None:
    secret = "artifact-test-secret"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    path = build_artifact_upload_path(
        "R-1",
        "P-1",
        "N-1",
        "S-1",
        secret,
        300,
        attempt_id="A-1",
        now=now,
    )
    qs = _query(path)
    assert verify_artifact_upload_path(
        "R-1",
        "P-1",
        "N-1",
        "S-1",
        "A-1",
        int(qs["expires"]),
        qs["signature"],
        secret,
        now=now,
    )
    assert not verify_artifact_upload_path(
        "R-1",
        "P-2",
        "N-1",
        "S-1",
        "A-1",
        int(qs["expires"]),
        qs["signature"],
        secret,
        now=now,
    )
    assert not verify_artifact_upload_path(
        "R-1",
        "P-1",
        "N-1",
        "S-1",
        "A-1",
        int(qs["expires"]),
        qs["signature"],
        secret,
        now=now + timedelta(seconds=301),
    )


def _seed_script(client, tmp_path) -> tuple[str, str]:
    """创建脚本记录与真实文件，返回 (签名路径, sha256)。"""
    container = client.app.state.container
    content = b"hello script content"
    script_file = tmp_path / "script.zip"
    script_file.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()

    with container.uow_factory()() as uow:
        user = uow.users.add(
            User(
                id=None,
                username="uploader",
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
                project_id="p1",
                project_key="P1",
                name="P",
                description="",
                status=ProjectStatus.ACTIVE,
                created_by=user.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        uow.test_scripts.add(
            TestScript(
                project_id="p1",
                script_id="S-dl-1",
                task_type="pytest",
                name="dl",
                version=1,
                file_ref=str(script_file),
                size=len(content),
                sha256=sha,
                config={},
                hardware_requirements=HardwareRequirements(),
                parse_status=ScriptParseStatus.PARSED,
                parse_location=ScriptParseLocation.MASTER,
                result_parse_location=ScriptParseLocation.MASTER,
                plugin_version="1.0.0",
                created_by=user.id,
            )
        )

    service = container.script_download_service()
    return service.build_path("S-dl-1"), sha


def test_download_script_with_valid_signature(client, tmp_path) -> None:
    path, sha = _seed_script(client, tmp_path)
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.content == b"hello script content"
    assert resp.headers["x-checksum-sha256"] == sha


def test_download_requires_valid_signature(client, tmp_path) -> None:
    path, _ = _seed_script(client, tmp_path)
    tampered = path.rsplit("signature=", 1)[0] + "signature=" + "0" * 64
    response = client.get(tampered)
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_FORBIDDEN"


def test_download_rejects_expired(client, tmp_path) -> None:
    path, _ = _seed_script(client, tmp_path)
    past = str(int(time.time()) - 10)
    tampered = re.sub(r"expires=\d+", f"expires={past}", path)
    assert client.get(tampered).status_code == 403


def test_download_script_not_found(client) -> None:
    service = client.app.state.container.script_download_service()
    path = service.build_path("S-noexist")
    resp = client.get(path)
    assert resp.status_code == 404
    assert resp.json()["code"] == "SCRIPT_NOT_FOUND"
