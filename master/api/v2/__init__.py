"""AETP V2 API 路由。"""

from .nodes import router as nodes_router
from .router import router

__all__ = ["nodes_router", "router"]
