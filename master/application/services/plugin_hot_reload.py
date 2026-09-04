"""Master 插件热重载器（热插拔）。

主模块一般不能重启，因此启用/停用/回滚/移除 Master 面插件后需要**不重启**就让进程内
装配生效。本服务把"当前 DB 的 ENABLED 插件集"重新投影到进程内各装配面：

- ``PluginRegistry``（内存注册表）：重载全部 ENABLED 版本；
- ``ExtensionResolver``：清空已解析缓存，使下次 resolve 重新加载/提取；
- Reporter/Analyzer 注册表：原地清空后按当前 ENABLED 插件重建；
- Notifier 渠道（SenderRegistry）：只卸载插件渠道、按当前 ENABLED 重建（保留内置）；
- Hook 注册表：清空后按当前 ENABLED HOOK 插件重建（准入/事件）。

``refresh()`` 是幂等全量重投影：任意一次状态变更（enable/disable/rollback/remove）
后调用一次即可，不依赖"重启落定"。启动时也用它做一次性装配。

Agent 侧插件（resource/runtime/software 等纯 agent、无 Master/UI 入口的）不在
Master 进程内装配；它们仍走 PENDING_RESTART + 节点同步（见 PluginGovernanceService）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aetp_protocol.plugin_types import PluginPoint, PluginStatus

from master.adapters.notifications.plugin_sender import PluginNotificationSender
from master.application.services.reporting_pipeline import (
    AnalyzerRegistry,
    ReporterRegistry,
)
from master.domain.repositories import UnitOfWork
from master.plugins.extension_resolver import ExtensionResolver
from master.plugins.registry import PluginRegistry

logger = logging.getLogger(__name__)


class PluginHotReloader:
    """把 DB 的 ENABLED 插件集投影到进程内各 Master 装配面（热插拔核心）。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        registry: PluginRegistry,
        extension_resolver: ExtensionResolver,
        reporter_registry: ReporterRegistry,
        analyzer_registry: AnalyzerRegistry,
        sender_registry,
        hook_runner,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._resolver = extension_resolver
        self._reporters = reporter_registry
        self._analyzers = analyzer_registry
        self._senders = sender_registry
        self._hook_runner = hook_runner

    def refresh(self) -> dict[str, int]:
        """全量重投影当前 ENABLED 插件到进程内装配面。返回各面计数。

        幂等：重复调用与当前状态一致时为无副作用空操作。任一步失败记录日志但
        不中断其它面（尽量让装配仍可用）。
        """
        stats: dict[str, int] = {
            "registry": 0,
            "reporters": 0,
            "analyzers": 0,
            "notifiers": 0,
            "hooks": 0,
        }
        with self._uow_factory() as uow:
            enabled = uow.plugin_versions.list(status=PluginStatus.ENABLED)
        try:
            self._registry.load(enabled)
            self._resolver.invalidate_all()
            stats["registry"] = len(enabled)
        except Exception:  # noqa: BLE001 - 单面失败不阻断
            logger.exception("插件热重载：PluginRegistry 重载失败")
        stats["reporters"] = self._rebuild_reporters()
        stats["analyzers"] = self._rebuild_analyzers()
        stats["notifiers"] = self._rebuild_notifiers()
        stats["hooks"] = self._rebuild_hooks()
        logger.info("插件热重载完成: %s", stats)
        return stats

    # ------------------------------------------------------------------ #
    # 各装配面重建
    # ------------------------------------------------------------------ #
    def _rebuild_reporters(self) -> int:
        try:
            self._reporters.clear()
            count = 0
            for resolved in self._resolver.resolve_all(PluginPoint.REPORTER):
                self._reporters.register(
                    resolved.plugin,
                    plugin_id=resolved.plugin_id,
                    plugin_version=resolved.plugin_version,
                )
                count += 1
            return count
        except Exception:  # noqa: BLE001
            logger.exception("插件热重载：Reporter 重建失败")
            return 0

    def _rebuild_analyzers(self) -> int:
        try:
            self._analyzers.clear()
            count = 0
            for resolved in self._resolver.resolve_all(PluginPoint.ANALYZER):
                self._analyzers.register(
                    resolved.plugin,
                    plugin_id=resolved.plugin_id,
                    plugin_version=resolved.plugin_version,
                )
                count += 1
            return count
        except Exception:  # noqa: BLE001
            logger.exception("插件热重载：Analyzer 重建失败")
            return 0

    def _rebuild_notifiers(self) -> int:
        try:
            self._senders.unregister_plugin_channels()
            count = 0
            for resolved in self._resolver.resolve_all(PluginPoint.NOTIFIER):
                self._senders.register_plugin(PluginNotificationSender(resolved.plugin))
                count += 1
            return count
        except Exception:  # noqa: BLE001
            logger.exception("插件热重载：Notifier 渠道重建失败")
            return 0

    def _rebuild_hooks(self) -> int:
        try:
            from master.adapters.hooks.plugin_hook import PluginAdmissionHook

            hook_registry = self._hook_runner.registry
            hook_registry.clear()
            count = 0
            for resolved in self._resolver.resolve_all(PluginPoint.HOOK):
                hook_registry.register_admission(PluginAdmissionHook(resolved.plugin))
                count += 1
            return count
        except Exception:  # noqa: BLE001
            logger.exception("插件热重载：Hook 重建失败")
            return 0


__all__ = ["PluginHotReloader"]
