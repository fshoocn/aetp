"""v1 API 路由汇总。"""

from fastapi import APIRouter

from master.api.v1.routes.admin import router as admin_router
from master.api.v1.routes.auth import router as auth_router
from master.api.v1.routes.projects import router as projects_router
from master.api.v1.routes.nodes import router as nodes_router
from master.api.v1.routes.project_devices import router as project_devices_router
from master.api.v1.routes.project_tasks import router as project_tasks_router
from master.api.v1.routes.runs import router as runs_router
from master.api.v1.routes.scripts import router as scripts_router
from master.api.v1.routes.test_tasks import router as test_tasks_router
from master.api.v1.routes.task_types import router as project_task_types_router
from master.api.v1.routes.assets import router as assets_router
from master.api.v1.routes.events import router as events_router
from master.api.v1.routes.internal import router as internal_router
from master.api.v1.routes.plugins import router as plugins_router
from master.api.v1.routes.notifications import (
    endpoints_router as notification_endpoints_router,
    subscriptions_router as event_subscriptions_router,
    deliveries_router as event_deliveries_router,
)
from master.api.v1.routes.schedules import router as schedules_router

router = APIRouter(prefix="/api/v1")
router.include_router(auth_router)
router.include_router(admin_router)
router.include_router(projects_router)
router.include_router(nodes_router)
router.include_router(project_devices_router)
router.include_router(project_tasks_router)
router.include_router(test_tasks_router)
router.include_router(project_task_types_router)
router.include_router(runs_router)
router.include_router(scripts_router)
router.include_router(assets_router)
router.include_router(events_router)
router.include_router(internal_router)
router.include_router(plugins_router)
router.include_router(notification_endpoints_router)
router.include_router(event_subscriptions_router)
router.include_router(event_deliveries_router)
router.include_router(schedules_router)
