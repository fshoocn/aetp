"""SSE（Server-Sent Events）适配器。"""

from __future__ import annotations

from .event import DomainEvent
from .event_bus import EventBus

__all__ = ["DomainEvent", "EventBus"]
