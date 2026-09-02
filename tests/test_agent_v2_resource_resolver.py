"""V2 resource 插件 Manifest/Resolver 装配测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aetp_protocol.ids import PluginId, SemVer, Sha256
from aetp_protocol.plugin_types import EntrypointRef, PluginPoint, PluginRef
from aetp_protocol.plugins import PluginEntrypoints, PluginManifest

from agent.application.services.v2_resource_provider_resolver import V2ResourceProviderResolver
from agent.plugins.v2_installer import InstalledV2Plugin
from agent.plugins.v2_registry import AgentV2PluginRegistry

PLUGIN_ID = PluginId("org.example.resource")
VERSION = SemVer("1.0.0")


def _register_resource_plugin(root: Path, *, provider_id: str = PLUGIN_ID.root) -> AgentV2PluginRegistry:
    install_path = root / PLUGIN_ID.root / VERSION.root
    agent_path = install_path / "agent"
    agent_path.mkdir(parents=True)
    manifest = PluginManifest(
        schema_version=2,
        id=PLUGIN_ID,
        version=VERSION,
        api_version=VERSION,
        point=PluginPoint.RESOURCE,
        display_name="Example Resource",
        entrypoints=PluginEntrypoints(agent=EntrypointRef("provider:create_provider")),
    )
    (install_path / "plugin.json").write_text(
        json.dumps(manifest.model_dump(mode="json")),
        encoding="utf-8",
    )
    (agent_path / "provider.py").write_text(
        f"""
class Provider:
    provider_id = {provider_id!r}
    resource_type = "example"

    def discover(self):
        return ()

    async def activate(self, binding):
        return None

    async def deactivate(self, binding):
        return None


def create_provider():
    return Provider()
""",
        encoding="utf-8",
    )
    registry = AgentV2PluginRegistry()
    registry.register(
        InstalledV2Plugin(
            ref=PluginRef(
                plugin_id=PLUGIN_ID,
                version=VERSION,
                archive_sha256=Sha256("a" * 64),
            ),
            manifest_path=install_path / "plugin.json",
            install_path=install_path,
        )
    )
    return registry


def test_resource_resolver_loads_manifest_resource_plugin(tmp_path: Path) -> None:
    resolver = V2ResourceProviderResolver(_register_resource_plugin(tmp_path))

    providers = resolver.resolve_all()

    assert len(providers) == 1
    assert providers[0].provider_id == PLUGIN_ID.root
    assert providers[0].resource_type == "example"
    assert resolver.resolve(PLUGIN_ID.root, VERSION.root) is providers[0]


def test_resource_resolver_rejects_provider_identity_mismatch(tmp_path: Path) -> None:
    resolver = V2ResourceProviderResolver(
        _register_resource_plugin(tmp_path, provider_id="org.other.resource")
    )

    assert resolver.resolve_all() == ()
    with pytest.raises(ValueError, match="provider_id"):
        resolver.resolve(PLUGIN_ID.root, VERSION.root)
