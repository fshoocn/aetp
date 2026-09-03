"""Quality Gate 准入 Hook 独立插件包（Master 面）。"""

from .quality_gate import QualityGateHook, create_hook

__all__ = ["QualityGateHook", "create_hook"]
