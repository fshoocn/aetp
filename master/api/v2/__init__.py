"""AETP V2 API 路由。"""

from .auth import router as auth_router
from .internal import router as internal_router
from .nodes import router as nodes_router
from .projects import router as projects_router
from .router import router
from .tasks import router as tasks_router

__all__ = ["auth_router", "internal_router", "nodes_router", "projects_router", "router", "tasks_router"]
