""" 插件版本生命周期纯函数。"""

from __future__ import annotations

from aetp_protocol.plugin_types import PluginStatus


class InvalidPluginStatusTransition(ValueError):
    """插件版本生命周期迁移非法。"""


_TRANSITIONS: dict[PluginStatus, frozenset[PluginStatus]] = {
    PluginStatus.UPLOADED: frozenset({PluginStatus.VERIFIED, PluginStatus.ERROR}),
    PluginStatus.VERIFIED: frozenset({PluginStatus.INSTALLED, PluginStatus.ERROR}),
    PluginStatus.INSTALLED: frozenset({PluginStatus.PENDING_RESTART, PluginStatus.ERROR}),
    PluginStatus.PENDING_RESTART: frozenset({PluginStatus.ENABLED, PluginStatus.DISABLED, PluginStatus.ERROR}),
    PluginStatus.ENABLED: frozenset({PluginStatus.PENDING_RESTART, PluginStatus.ERROR}),
    PluginStatus.DISABLED: frozenset({PluginStatus.PENDING_RESTART, PluginStatus.REMOVED, PluginStatus.ERROR}),
    PluginStatus.ERROR: frozenset({PluginStatus.VERIFIED, PluginStatus.REMOVED}),
    PluginStatus.REMOVED: frozenset(),
}


def can_transition(status: PluginStatus, target: PluginStatus) -> bool:
    """判断插件版本状态迁移是否合法。"""
    return target in _TRANSITIONS[status]


def assert_transition(status: PluginStatus, target: PluginStatus) -> None:
    """校验插件版本状态迁移，非法时抛出明确异常。"""
    if not can_transition(status, target):
        raise InvalidPluginStatusTransition(f"非法插件状态迁移: {status.value} -> {target.value}")


def transitions_for(status: PluginStatus) -> frozenset[PluginStatus]:
    """返回某状态允许的目标状态集合。"""
    return _TRANSITIONS[status]
