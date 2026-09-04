"""Master 节点插件对账与卸载服务。

职责：
- 对账（reconcile）：比较某节点的插件期望版本（``agent_plugin_desired_versions``）
  与 Agent 最新能力快照上报的插件库存（``plugin_inventory``），自动生成
  安装/卸载同步命令并经 ``PluginSyncService`` 下发；
- 卸载（uninstall）：显式把某个插件版本（或整个插件）从单个节点卸载，
  或在治理移除插件版本后从所有装有它的在线节点卸载。

对账规则：
- Agent 已装、无期望或期望版本不同 → REMOVE 已装版本；
- 有期望、Agent 未装该版本 → INSTALL（包引用按治理归档 SHA-256 生成）；
- 期望版本在 Master 已不存在/被移除 → 清除期望并卸载 Agent 上该插件全部版本。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aetp_protocol.ids import BusinessId, PluginId, SemVer
from aetp_protocol.plugin_types import PluginDistributionRef, PluginPoint, PluginStatus, PluginSyncAction
from aetp_protocol.plugins import PluginSyncItem

from master.application.services.plugin_sync_service import PluginSyncService
from master.domain.models import AgentPluginSyncOperationRecord
from master.domain.repositories import UnitOfWork

logger = logging.getLogger(__name__)


class PluginNotInstalledOnNode(KeyError):
    """目标节点没有安装要卸载的插件版本。"""


class NodePluginReconciler:
    """按节点对账插件期望与实际库存，并执行显式卸载。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        sync_service: PluginSyncService,
        *,
        package_url_builder: Callable[[PluginId, SemVer], str] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._sync = sync_service
        self._package_url_builder = package_url_builder

    def reconcile_node(
        self,
        node_id: BusinessId,
        *,
        actor_id: int | None = None,
    ) -> AgentPluginSyncOperationRecord | None:
        """对账单个节点；无差异时返回 None（不下发同步）。"""
        items = self._plan_node(node_id)
        if not items:
            return None
        return self._sync.request(node_id, items, restart_after=True, actor_id=actor_id)

    def uninstall_plugin_from_node(
        self,
        node_id: BusinessId,
        plugin_id: PluginId,
        *,
        version: SemVer | None = None,
        actor_id: int | None = None,
    ) -> AgentPluginSyncOperationRecord:
        """从单个节点卸载插件：version 为 None 时卸载该插件全部已装版本。

        同时清除节点上指向被卸载版本的期望，避免随后被对账重新装回。
        """
        with self._uow_factory() as uow:
            inventory = self._inventory(uow, node_id)
            installed = tuple(
                item
                for item in inventory
                if item.plugin_id == plugin_id and (version is None or item.version == version)
            )
            if not installed:
                target = f"{plugin_id.root}@{version.root}" if version is not None else plugin_id.root
                raise PluginNotInstalledOnNode(f"节点 {node_id.root} 未安装插件: {target}")
            desired = uow.agent_plugin_desired_versions.get(node_id, plugin_id)
            if desired is not None and (version is None or desired.desired.version == version):
                uow.agent_plugin_desired_versions.remove(node_id, plugin_id)
        items = tuple(
            PluginSyncItem(
                plugin_id=item.plugin_id,
                point=item.point,
                version=item.version,
                action=PluginSyncAction.REMOVE,
            )
            for item in installed
        )
        return self._sync.request(node_id, items, restart_after=True, actor_id=actor_id)

    def uninstall_plugin_everywhere(
        self,
        plugin_id: PluginId,
        version: SemVer,
        *,
        actor_id: int | None = None,
    ) -> tuple[AgentPluginSyncOperationRecord, ...]:
        """把某插件版本从所有装有它的节点上卸载（治理移除后的自动清理）。

        - 所有节点上指向该版本的期望都会被清除（含离线节点，防止对账复装）；
        - 在线且快照库存含该版本的节点逐个下发 REMOVE（离线节点跳过并记录日志）。
        """
        targets: list[tuple[BusinessId, PluginPoint]] = []
        with self._uow_factory() as uow:
            for node in uow.nodes.list_all():
                if node.id is None:
                    continue
                node_id = BusinessId(node.node_id)
                desired = uow.agent_plugin_desired_versions.get(node_id, plugin_id)
                if desired is not None and desired.desired.version == version:
                    uow.agent_plugin_desired_versions.remove(node_id, plugin_id)
                snapshot = uow.node_capability_snapshots.get_latest(node_id)
                if snapshot is None or not node.online:
                    continue
                point = next(
                    (
                        item.point
                        for item in snapshot.snapshot.plugin_inventory
                        if item.plugin_id == plugin_id and item.version == version
                    ),
                    None,
                )
                if point is not None:
                    targets.append((node_id, point))
        dispatched: list[AgentPluginSyncOperationRecord] = []
        for node_id, point in targets:
            item = PluginSyncItem(
                plugin_id=plugin_id,
                point=point,
                version=version,
                action=PluginSyncAction.REMOVE,
            )
            try:
                dispatched.append(
                    self._sync.request(node_id, (item,), restart_after=True, actor_id=actor_id)
                )
            except Exception:  # noqa: BLE001 - 单节点失败不影响其余节点
                logger.warning(
                    "插件卸载下发失败: node=%s plugin=%s@%s", node_id.root, plugin_id.root, version.root
                )
        return tuple(dispatched)

    def _plan_node(self, node_id: BusinessId) -> tuple[PluginSyncItem, ...]:
        installs: list[PluginSyncItem] = []
        removals: list[PluginSyncItem] = []
        removal_keys: set[tuple[str, str]] = set()
        with self._uow_factory() as uow:
            desired = {
                record.desired.plugin_id.root: record.desired
                for record in uow.agent_plugin_desired_versions.list_by_node(node_id)
            }
            inventory = self._inventory(uow, node_id)
            installed_by_plugin: dict[str, list[PluginSyncItem]] = {}
            for item in inventory:
                installed_by_plugin.setdefault(item.plugin_id.root, []).append(item)
                wanted = desired.get(item.plugin_id.root)
                if wanted is None or wanted.version != item.version:
                    removal_keys.add((item.plugin_id.root, item.version.root))
                    removals.append(
                        PluginSyncItem(
                            plugin_id=item.plugin_id,
                            point=item.point,
                            version=item.version,
                            action=PluginSyncAction.REMOVE,
                        )
                    )
            for plugin_key, wanted in desired.items():
                plugin_id = wanted.plugin_id
                record = uow.plugin_versions.get(plugin_id, wanted.version)
                if record is None or record.status is PluginStatus.REMOVED:
                    # 期望版本在 Master 已不可用：清期望并卸载 Agent 上的全部版本。
                    uow.agent_plugin_desired_versions.remove(node_id, plugin_id)
                    for item in installed_by_plugin.get(plugin_key, ()):
                        if (item.plugin_id.root, item.version.root) not in removal_keys:
                            removals.append(
                                PluginSyncItem(
                                    plugin_id=item.plugin_id,
                                    point=item.point,
                                    version=item.version,
                                    action=PluginSyncAction.REMOVE,
                                )
                            )
                    continue
                installed_versions = {item.version.root for item in installed_by_plugin.get(plugin_key, ())}
                if wanted.version.root not in installed_versions:
                    installs.append(
                        PluginSyncItem(
                            plugin_id=plugin_id,
                            point=wanted.point,
                            version=wanted.version,
                            action=PluginSyncAction.INSTALL,
                            package=PluginDistributionRef(
                                plugin_id=plugin_id,
                                version=wanted.version,
                                archive_sha256=record.archive_sha256,
                                download_url=(
                                    self._package_url_builder(plugin_id, wanted.version)
                                    if self._package_url_builder is not None
                                    else None
                                ),
                            ),
                        )
                    )
        return tuple(installs) + tuple(removals)

    @staticmethod
    def _inventory(uow: UnitOfWork, node_id: BusinessId) -> tuple:
        snapshot = uow.node_capability_snapshots.get_latest(node_id)
        if snapshot is None:
            return ()
        return snapshot.snapshot.plugin_inventory
