""" resource 插件 Manifest/Resolver 装配测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aetp_protocol.ids import PluginId, SemVer, Sha256
from aetp_protocol.plugin_types import EntrypointRef, PluginPoint, PluginRef
from aetp_protocol.plugins import PluginEntrypoints, PluginManifest

from agent.application.services.resource_provider_resolver import ResourceProviderResolver
from agent.plugins.installer import InstalledPlugin
from agent.plugins.registry import PluginRegistry

PLUGIN_ID = PluginId("org.example.resource")
VERSION = SemVer("1.0.0")


def _register_resource_plugin(root: Path, *, provider_id: str = PLUGIN_ID.root) -> PluginRegistry:
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
    registry = PluginRegistry()
    registry.register(
        InstalledPlugin(
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
    resolver = ResourceProviderResolver(_register_resource_plugin(tmp_path))

    providers = resolver.resolve_all()

    assert len(providers) == 1
    assert providers[0].provider_id == PLUGIN_ID.root
    assert providers[0].resource_type == "example"
    assert resolver.resolve(PLUGIN_ID.root, VERSION.root) is providers[0]


def _register_multi_provider_plugin(root: Path) -> PluginRegistry:
    """注册一个 agent 入口返回多个 Provider（一包多 provider）的 resource 插件。"""
    install_path = root / PLUGIN_ID.root / VERSION.root
    agent_path = install_path / "agent"
    agent_path.mkdir(parents=True)
    manifest = PluginManifest(
        schema_version=2,
        id=PLUGIN_ID,
        version=VERSION,
        api_version=VERSION,
        point=PluginPoint.RESOURCE,
        display_name="Example Multi Resource",
        entrypoints=PluginEntrypoints(agent=EntrypointRef("provider:create_providers")),
    )
    (install_path / "plugin.json").write_text(
        json.dumps(manifest.model_dump(mode="json")),
        encoding="utf-8",
    )
    (agent_path / "provider.py").write_text(
        f"""
class _Base:
    def discover(self):
        return ()

    async def activate(self, binding):
        return None

    async def deactivate(self, binding):
        return None


class Serial(_Base):
    provider_id = "{PLUGIN_ID.root}.serial"
    resource_type = "serial"


class Power(_Base):
    provider_id = "{PLUGIN_ID.root}.power"
    resource_type = "power"


class Can(_Base):
    provider_id = "{PLUGIN_ID.root}.can"
    resource_type = "can"


def create_providers():
    return (Serial(), Power(), Can())
""",
        encoding="utf-8",
    )
    registry = PluginRegistry()
    registry.register(
        InstalledPlugin(
            ref=PluginRef(
                plugin_id=PLUGIN_ID,
                version=VERSION,
                archive_sha256=Sha256("b" * 64),
            ),
            manifest_path=install_path / "plugin.json",
            install_path=install_path,
        )
    )
    return registry


def test_resource_resolver_expands_multi_provider_plugin(tmp_path: Path) -> None:
    resolver = ResourceProviderResolver(_register_multi_provider_plugin(tmp_path))

    providers = resolver.resolve_all()

    assert len(providers) == 3
    assert {p.provider_id for p in providers} == {
        "org.example.resource.serial",
        "org.example.resource.power",
        "org.example.resource.can",
    }
    assert {p.resource_type for p in providers} == {"serial", "power", "can"}
    # resolve() 兼容：返回该包首个 Provider
    assert resolver.resolve(PLUGIN_ID.root, VERSION.root) is providers[0]


def test_resource_resolver_rejects_duplicate_provider_id(tmp_path: Path) -> None:
    install_path = tmp_path / PLUGIN_ID.root / VERSION.root
    agent_path = install_path / "agent"
    agent_path.mkdir(parents=True)
    manifest = PluginManifest(
        schema_version=2,
        id=PLUGIN_ID,
        version=VERSION,
        api_version=VERSION,
        point=PluginPoint.RESOURCE,
        display_name="Example Bad Resource",
        entrypoints=PluginEntrypoints(agent=EntrypointRef("provider:create_providers")),
    )
    (install_path / "plugin.json").write_text(
        json.dumps(manifest.model_dump(mode="json")),
        encoding="utf-8",
    )
    (agent_path / "provider.py").write_text(
        """
class Can:
    provider_id = "com.vector.can-resource"
    resource_type = "can"

    def discover(self):
        return ()

    async def activate(self, binding):
        return None

    async def deactivate(self, binding):
        return None


def create_providers():
    return (Can(), Can())
""",
        encoding="utf-8",
    )
    registry = PluginRegistry()
    registry.register(
        InstalledPlugin(
            ref=PluginRef(
                plugin_id=PLUGIN_ID,
                version=VERSION,
                archive_sha256=Sha256("c" * 64),
            ),
            manifest_path=install_path / "plugin.json",
            install_path=install_path,
        )
    )
    resolver = ResourceProviderResolver(registry)

    assert resolver.resolve_all() == ()
    with pytest.raises(ValueError, match="重复"):
        resolver.resolve(PLUGIN_ID.root, VERSION.root)
