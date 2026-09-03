"""org.aetp.python-runtime 插件 agent 入口。

工厂 ``create_providers()`` 返回一个 PythonRuntimeProvider。Agent
``EnvironmentProviderResolver`` 会加载并归入能力快照 runtime 分区。
"""

from __future__ import annotations

from aetp_protocol.discovery import RuntimeProvider

from .runtime import PythonRuntimeProvider


def create_providers() -> tuple[RuntimeProvider, ...]:
    return (PythonRuntimeProvider(),)


__all__ = ["create_providers"]
