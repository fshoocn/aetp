"""通知 Sender Adapters 包。"""

from .senders import SenderRegistry, build_default_registry

__all__ = ["SenderRegistry", "build_default_registry"]
