"""Master V2 Reporter/Analyzer 归档入口解析测试。"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from aetp_protocol.ids import PluginId, SemVer, Sha256
from aetp_protocol.plugin_types import PluginPoint, PluginStatus
from aetp_protocol.plugins import EntrypointRef, PluginEntrypoints, PluginManifest

from master.domain.models import PluginVersionRecord
from master.plugins.v2_extension_resolver import MasterV2ExtensionResolver
from master.plugins.v2_registry import V2PluginRegistry


def _record(tmp_path: Path) -> tuple[V2PluginRegistry, PluginVersionRecord, Path]:
    archive_path = tmp_path / "archives" / "archive.zip"
    archive_path.parent.mkdir()
    manifest = PluginManifest(
        schema_version=2,
        id=PluginId("org.test.reporter"),
        version=SemVer("1.0.0"),
        api_version=SemVer("2.0.0"),
        point=PluginPoint.REPORTER,
        display_name="Test Reporter",
        entrypoints=PluginEntrypoints(master=EntrypointRef("reporter:create_reporter")),
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", manifest.model_dump_json())
        archive.writestr(
            "master/reporter.py",
            "class Reporter:\n"
            "    async def report(self, request, context):\n"
            "        return None\n"
            "\n"
            "def create_reporter():\n"
            "    return Reporter()\n",
        )
    archive_path.write_bytes(buffer.getvalue())
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    registry = V2PluginRegistry(tmp_path / "archives")
    record = PluginVersionRecord(
        id=None,
        plugin_id=manifest.id,
        version=manifest.version,
        point=manifest.point,
        status=PluginStatus.ENABLED,
        filename="reporter.zip",
        archive_sha256=Sha256(digest),
        manifest_sha256=Sha256("a" * 64),
        manifest=manifest,
        archive_path=str(archive_path),
        installed_at=None,
        created_at=None,
        updated_at=None,
    )
    registry.register(record)
    return registry, record, archive_path


def test_master_resolver_loads_manifest_entrypoint_and_caches(tmp_path) -> None:
    registry, record, _archive_path = _record(tmp_path)
    resolver = MasterV2ExtensionResolver(registry, tmp_path / "runtime")

    first = resolver.resolve(record, PluginPoint.REPORTER)
    second = resolver.resolve(record, PluginPoint.REPORTER)

    assert first.plugin_id == "org.test.reporter"
    assert "report" in dir(first.plugin)
    assert second is first
    assert (tmp_path / "runtime" / "org.test.reporter" / "1.0.0" / "master" / "reporter.py").is_file()
