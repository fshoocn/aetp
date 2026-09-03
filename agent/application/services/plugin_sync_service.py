"""Agent  插件同步应用服务。"""

from __future__ import annotations

from typing import Protocol

from aetp_protocol.errors import ErrorCode
from aetp_protocol.ids import PluginId, SemVer, SessionId
from aetp_protocol.plugin_types import PluginDistributionRef, PluginSyncAction
from aetp_protocol.plugins import PluginSyncItem, PluginSyncItemResult, PluginSyncRequest, PluginSyncResult

from agent.plugins.errors import PluginInstallError
from agent.plugins.installer import InstalledPlugin
from agent.plugins.registry import PluginRegistry


class PluginInstallPort(Protocol):
    """Agent  插件安装端口。"""

    def install(self, package: PluginDistributionRef) -> InstalledPlugin: ...

    def remove(self, plugin_id: PluginId, version: SemVer) -> None: ...


class AgentPluginSyncService:
    """执行 Master 下发的  插件同步请求。"""

    def __init__(
        self,
        installer: PluginInstallPort,
        current_session_id: SessionId,
        registry: PluginRegistry | None = None,
    ) -> None:
        self._installer = installer
        self._current_session_id = current_session_id
        self._registry = registry

    def apply(self, request: PluginSyncRequest) -> PluginSyncResult:
        if request.expected_session_id != self._current_session_id:
            return PluginSyncResult(
                sync_id=request.sync_id,
                node_id=request.node_id,
                accepted=False,
                restart_required=False,
                items=tuple(
                    self._failure(item, ErrorCode("STALE_SESSION"), "Agent 会话已变化")
                    for item in request.items
                ),
            )

        results: list[PluginSyncItemResult] = []
        changed = False
        for item in request.items:
            try:
                if item.action is PluginSyncAction.REMOVE:
                    self._installer.remove(item.plugin_id, item.version)
                    if self._registry is not None:
                        self._registry.remove(item.plugin_id.root, item.version.root)
                    results.append(
                        PluginSyncItemResult(
                            plugin_id=item.plugin_id,
                            version=item.version,
                            state="removed",
                        )
                    )
                else:
                    if item.package is None:
                        raise PluginInstallError("安装类同步项缺少 package")
                    installed = self._installer.install(item.package)
                    if self._registry is not None:
                        self._registry.register(installed)
                    results.append(
                        PluginSyncItemResult(
                            plugin_id=item.plugin_id,
                            version=item.version,
                            state="installed",
                        )
                    )
                changed = True
            except PluginInstallError as exc:
                results.append(
                    self._failure(item, ErrorCode("PLUGIN_SYNC_FAILED"), str(exc))
                )

        accepted = all(result.state not in {"failed", "blocked"} for result in results)
        return PluginSyncResult(
            sync_id=request.sync_id,
            node_id=request.node_id,
            accepted=accepted,
            restart_required=changed and request.restart_after,
            items=tuple(results),
        )

    @staticmethod
    def _failure(
        item: PluginSyncItem,
        code: ErrorCode,
        message: str,
    ) -> PluginSyncItemResult:
        return PluginSyncItemResult(
            plugin_id=item.plugin_id,
            version=item.version,
            state="failed",
            unavailable_reasons=(code,),
            message=message,
        )
