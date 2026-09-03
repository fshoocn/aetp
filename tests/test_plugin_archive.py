"""插件归档完整性与入口校验测试。"""

from __future__ import annotations

import io
import json
import stat
import zipfile

import pytest
from aetp_protocol.plugin_archive import PluginArchiveVerifier
from aetp_protocol.plugin_types import PluginStatus

from master.plugins.lifecycle import (
    InvalidPluginStatusTransition,
    assert_transition,
)


def _archive(*, extra: dict[str, bytes] | None = None) -> bytes:
    files = {
        "plugin.json": json.dumps(
            {
                "schema_version": 2,
                "id": "org.example.executor",
                "version": "2.0.0",
                "api_version": "2.0.0",
                "point": "executor",
                "display_name": "Example Executor",
                "entrypoints": {
                    "master": "plugin:create_plugin",
                    "agent": "plugin:create_plugin",
                },
            }
        ).encode(),
        "master/plugin.py": b"def create_plugin(): pass",
        "agent/plugin.py": b"def create_plugin(): pass",
    }
    files.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_archive_verifier_returns_manifest_and_checksum() -> None:
    result = PluginArchiveVerifier().verify(_archive(), filename="example.zip")

    assert result.manifest.id.root == "org.example.executor"
    assert len(result.sha256.root) == 64
    assert "master/plugin.py" in result.members


def test_archive_verifier_rejects_path_traversal() -> None:
    with pytest.raises(ValueError, match="unsafe path"):
        PluginArchiveVerifier().verify(_archive(extra={"../escape.py": b"bad"}), filename="example.zip")


def test_archive_verifier_rejects_symlink() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("agent/plugin.py")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../../outside.py")
        archive.writestr(
            "plugin.json",
            json.dumps(
                {
                    "schema_version": 2,
                    "id": "org.example.executor",
                    "version": "2.0.0",
                    "api_version": "2.0.0",
                    "point": "executor",
                    "display_name": "Example Executor",
                    "entrypoints": {"agent": "plugin:create_plugin", "master": "plugin:create_plugin"},
                }
            ),
        )
        archive.writestr("master/plugin.py", b"ok")

    with pytest.raises(ValueError, match="符号链接"):
        PluginArchiveVerifier().verify(buffer.getvalue(), filename="example.zip")


def test_archive_verifier_rejects_missing_manifest_entrypoint_file() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "plugin.json",
            json.dumps(
                {
                    "schema_version": 2,
                    "id": "org.example.executor",
                    "version": "2.0.0",
                    "api_version": "2.0.0",
                    "point": "executor",
                    "display_name": "Example Executor",
                    "entrypoints": {"agent": "plugin:create_plugin", "master": "plugin:create_plugin"},
                }
            ),
        )
        archive.writestr("master/plugin.py", b"ok")

    with pytest.raises(ValueError, match="agent 入口文件不存在"):
        PluginArchiveVerifier().verify(buffer.getvalue(), filename="example.zip")


def test_plugin_status_lifecycle_is_fail_closed() -> None:
    assert_transition(PluginStatus.UPLOADED, PluginStatus.VERIFIED)
    assert_transition(PluginStatus.VERIFIED, PluginStatus.INSTALLED)
    assert_transition(PluginStatus.INSTALLED, PluginStatus.PENDING_RESTART)
    assert_transition(PluginStatus.PENDING_RESTART, PluginStatus.ENABLED)
    assert_transition(PluginStatus.ENABLED, PluginStatus.PENDING_RESTART)
    assert_transition(PluginStatus.PENDING_RESTART, PluginStatus.DISABLED)
    assert_transition(PluginStatus.DISABLED, PluginStatus.REMOVED)
    with pytest.raises(InvalidPluginStatusTransition):
        assert_transition(PluginStatus.REMOVED, PluginStatus.ENABLED)
