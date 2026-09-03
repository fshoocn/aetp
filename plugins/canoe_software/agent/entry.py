"""org.aetp.canoe-software 插件 agent 入口。

工厂 ``create_providers()`` 返回一个 CanoeSoftwareProvider，映射文件默认为
已安装插件目录下 ``agent/canoe_software.json``。Agent
``EnvironmentProviderResolver`` 会加载并归入能力快照 software 分区。
"""

from __future__ import annotations

from pathlib import Path

from aetp_protocol.discovery import SoftwareProvider

from .canoe_software import DEFAULT_CANOE_MAP_FILE, CanoeSoftwareProvider


def create_providers() -> tuple[SoftwareProvider, ...]:
    agent_dir = Path(__file__).resolve().parent
    return (CanoeSoftwareProvider(agent_dir / DEFAULT_CANOE_MAP_FILE),)


__all__ = ["create_providers"]
