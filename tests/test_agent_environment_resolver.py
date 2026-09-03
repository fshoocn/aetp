"""runtime/software 插件 Manifest/Resolver 装配测试。"""

from __future__ import annotations

import json
from pathlib import Path

from aetp_protocol.ids import PluginId, SemVer, Sha256
from aetp_protocol.plugin_types import EntrypointRef, PluginPoint, PluginRef
from aetp_protocol.plugins import PluginEntrypoints, PluginManifest

from agent.application.services.environment_provider_resolver import EnvironmentProviderResolver
from agent.plugins.installer import InstalledPlugin
from agent.plugins.registry import PluginRegistry

PLUGIN_ID = PluginId("org.example.env")
VERSION = SemVer("1.0.0")


def _install(
    root: Path,
    *,
    point: PluginPoint,
    entrypoint: str,
    code: str,
    plugin_id: PluginId = PLUGIN_ID,
    sha_seed: str = "a",
    registry: PluginRegistry | None = None,
) -> PluginRegistry:
    install_path = root / plugin_id.root / VERSION.root
    agent_path = install_path / "agent"
    agent_path.mkdir(parents=True)
    manifest = PluginManifest(
        schema_version=2,
        id=plugin_id,
        version=VERSION,
        api_version=VERSION,
        point=point,
        display_name=f"Example {point.value}",
        entrypoints=PluginEntrypoints(agent=EntrypointRef(entrypoint)),
    )
    (install_path / "plugin.json").write_text(
        json.dumps(manifest.model_dump(mode="json")),
        encoding="utf-8",
    )
    (agent_path / "provider.py").write_text(code, encoding="utf-8")
    registry = registry or PluginRegistry()
    registry.register(
        InstalledPlugin(
            ref=PluginRef(
                plugin_id=plugin_id,
                version=VERSION,
                archive_sha256=Sha256(sha_seed * 64),
            ),
            manifest_path=install_path / "plugin.json",
            install_path=install_path,
        )
    )
    return registry


_RUNTIME_CODE = """
class PythonProvider:
    provider_id = "org.example.env"
    runtime_type = "python"

    def discover(self):
        return ()


class DotnetProvider:
    provider_id = "org.example.env.dotnet"
    runtime_type = "dotnet"

    def discover(self):
        return ()


def create_providers():
    return (PythonProvider(), DotnetProvider())
"""


def test_runtime_resolver_loads_multi_provider_plugin(tmp_path: Path) -> None:
    resolver = EnvironmentProviderResolver(
        _install(tmp_path, point=PluginPoint.RUNTIME, entrypoint="provider:create_providers", code=_RUNTIME_CODE)
    )

    providers = resolver.runtime_providers()

    assert len(providers) == 2
    assert {p.provider_id for p in providers} == {
        "org.example.env",
        "org.example.env.dotnet",
    }
    assert {p.runtime_type for p in providers} == {"python", "dotnet"}
    assert resolver.software_providers() == ()


_SOFTWARE_CODE = """
class CanoeProvider:
    provider_id = "org.example.env.canoe"
    name = "CANoe"

    def discover(self):
        return ()


def create_providers():
    return (CanoeProvider(),)
"""


def test_software_resolver_loads_plugin(tmp_path: Path) -> None:
    resolver = EnvironmentProviderResolver(
        _install(tmp_path, point=PluginPoint.SOFTWARE, entrypoint="provider:create_providers", code=_SOFTWARE_CODE)
    )

    providers = resolver.software_providers()

    assert len(providers) == 1
    assert providers[0].provider_id == "org.example.env.canoe"
    assert providers[0].name == "CANoe"
    assert resolver.runtime_providers() == ()


def test_resolver_skips_unrelated_points_and_isolates_bad_plugins(tmp_path: Path) -> None:
    registry = PluginRegistry()
    _install(
        tmp_path,
        point=PluginPoint.SOFTWARE,
        entrypoint="provider:create_providers",
        code=_SOFTWARE_CODE,
        registry=registry,
    )
    # 另一个 software 插件：agent 入口抛异常，不应影响上面的正常插件
    _install(
        tmp_path,
        point=PluginPoint.SOFTWARE,
        entrypoint="provider:broken",
        code="def broken():\n    raise RuntimeError('boom')\n",
        plugin_id=PluginId("org.example.broken"),
        sha_seed="b",
        registry=registry,
    )

    resolver = EnvironmentProviderResolver(registry)

    assert len(resolver.software_providers()) == 1
    assert resolver.runtime_providers() == ()


def test_resolver_rejects_non_provider_entrypoint_result(tmp_path: Path) -> None:
    registry = _install(
        tmp_path,
        point=PluginPoint.SOFTWARE,
        entrypoint="provider:create_providers",
        code="""
def create_providers():
    return "not-a-provider"
""",
    )

    resolver = EnvironmentProviderResolver(registry)

    assert resolver.software_providers() == ()
    assert resolver.runtime_providers() == ()
