"""FastAPI 应用组装。

由 run.py 启动 uvicorn 加载本模块的 app。
lifespan 中创建依赖注入容器并初始化（建表 + 结构同步），
容器实例注入 app.state，路由经 deps 从容器获取依赖。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from aetp_protocol.plugin_types import PluginStatus
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

from master.config import get_settings

from .api.v1.dependencies import DbDep
from .api.v1.errors import register_application_error_handlers
from .api.v1.router import router as v1_router
from .api.v2.nodes import router as v2_nodes_router
from .api.v2.router import router as v2_router
from .api.v2.tasks import router as v2_tasks_router
from .bootstrap.container import Container

logger = logging.getLogger(__name__)


def _bootstrap_admin(app: FastAPI) -> None:
    """若 users 表为空且配置了管理员凭据，自动创建首个平台管理员。"""
    settings = get_settings()
    if not settings.bootstrap_admin_username or not settings.bootstrap_admin_password:
        logger.debug("未配置 bootstrap 管理员，跳过管理员初始化")
        return
    container: Container = app.state.container
    logger.info("开始检查 bootstrap 管理员: %s", settings.bootstrap_admin_username)
    try:
        created = container.auth_service().bootstrap_admin(
            settings.bootstrap_admin_username,
            settings.bootstrap_admin_password,
            settings.bootstrap_admin_display_name,
        )
    except IntegrityError:
        logger.info(
            "平台管理员 bootstrap 已由其他实例完成: %s",
            settings.bootstrap_admin_username,
        )
        return
    if created:
        logger.info(
            "已创建首个平台管理员: %s (角色=admin, 状态=active)",
            settings.bootstrap_admin_username,
        )


def _web_dist_dir() -> Path | None:
    """返回前端 web/dist 构建产物的绝对路径。

    两种情况：
    - 源码运行：返回项目根目录下的 web/dist
    - exe 冻结运行：返回 sys.executable 同目录下的 web/

    不存在时返回 None，各路由仍可正常提供 API（仅无前端页面）。
    """
    if getattr(sys, "frozen", False):
        p = Path(sys.executable).parent / "web"
    else:
        p = Path(__file__).resolve().parents[1] / "web" / "dist"
    return p if p.exists() else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期管理：创建 DI 容器、初始化数据库、挂载前端。

    启动时：
    1. 创建 Container 依赖注入容器
    2. 解析 container.database() 单例 → 触发自动建表/迁移
    3. 注入 app.state.container 供路由依赖使用

    关闭时：
    1. 释放数据库连接池
    """
    logger.info("应用生命周期启动")
    container = Container()
    logger.debug("依赖注入容器已创建")
    # 启动时校验安全配置（JWT 密钥强度等），弱配置直接拒绝启动。
    # 校验只在这里做一次：无论从 run.py / 直接 uvicorn / 测试 / 嵌入方式启动，
    # lifespan 都会执行，保证所有入口都经过同一道启动检查。
    from master.api.v1.security import validate_security_settings

    validate_security_settings()
    # 首次解析 database 单例 — 触发 _init_database() → create_database().connect() → Alembic upgrade head
    logger.info("开始初始化数据库和执行迁移")
    container.database()
    logger.info("数据库初始化完成")
    with container.uow_factory()() as uow:
        container.v2_plugin_registry().load(
            uow.plugin_versions.list(status=PluginStatus.ENABLED)
        )
    # Master 插件注册表在应用启动时加载：脚本解析、验证、分片和硬件需求
    # 均由 Master 侧插件提供；Agent 只加载对应执行包。
    plugins = container.plugin_registry()
    logger.info(
        "Master 插件注册表已加载: task_types=%s",
        [package.metadata.task_type for package in plugins.list()],
    )
    app.state.container = container

    # 平台管理员 bootstrap：若 users 表为空且配置了管理员凭据，自动创建首个 admin
    _bootstrap_admin(app)

    # P6.4：Master MQTT 运行时（订阅 Agent 事件 → 路由投影 + outbox 发送）。
    # 仅在显式配置了 MQTT broker 时才启动；否则跳过（纯 HTTP/单测模式）。
    settings = get_settings()
    if settings.mqtt_host:
        runtime = container.mqtt_runtime()
        await runtime.start()
        app.state.mqtt_runtime = runtime
        logger.info("Master MQTT 运行时已启动: broker=%s:%s", settings.mqtt_host, settings.mqtt_port)
    else:
        logger.info("未配置 MQTT broker，跳过 Master MQTT 运行时")

    # 启动恢复：重置节点投影/会话 + 扫描遗留非终态 Run，超时标记 + 孤儿 Shard 转 waiting_recovery
    recovery = container.recovery_service()
    stats = recovery.startup_recovery()
    if any(stats.values()):
        logger.warning("启动恢复完成: %s", stats)
    else:
        logger.info("启动恢复：无需恢复")

    # 后台维护 worker：Schedule tick（定时触发）+ Stale Run 超时检测 + 孤儿清理。
    # 仅在有 MQTT broker 或需要定时调度的完整部署下才启动；纯 HTTP/单测
    # 模式仍可关闭（由 MQTT runtime 一起控制，保持一致的生命周期边界）。
    maintenance_worker = container.maintenance_worker()
    await maintenance_worker.start()
    app.state.maintenance_worker = maintenance_worker
    logger.info("后台维护 worker 已启动")

    # 事件 Hook 后台消费 worker：异步执行 Event Hook，不阻塞 SSE/通知。
    event_hook_worker = container.event_hook_worker()
    await event_hook_worker.start()
    app.state.event_hook_worker = event_hook_worker
    logger.info("事件 Hook worker 已启动")

    logger.info("应用启动准备完成")

    yield

    # 1. 关闭 SSE 事件总线：唤醒所有阻塞的 SSE 连接，让它们优雅退出
    event_bus = container.event_bus()
    await event_bus.shutdown()
    logger.info("SSE 事件总线已关闭")

    mqtt_runtime = getattr(app.state, "mqtt_runtime", None)
    if mqtt_runtime is not None:
        try:
            # Broker 断开、SSE 连接或 aiomqtt 消费任务异常时，关闭路径不能
            # 无限等待；资源清理应有界，避免 Master 进程卡在退出阶段。
            await asyncio.wait_for(mqtt_runtime.stop(), timeout=5.0)
        except TimeoutError:
            logger.error("Master MQTT 运行时关闭超时，继续释放数据库资源")
        except Exception:
            logger.exception("Master MQTT 运行时关闭失败，继续释放数据库资源")

    maintenance_worker = getattr(app.state, "maintenance_worker", None)
    if maintenance_worker is not None:
        try:
            await asyncio.wait_for(maintenance_worker.stop(), timeout=5.0)
        except TimeoutError:
            logger.error("后台维护 worker 关闭超时，继续释放数据库资源")
        except Exception:
            logger.exception("后台维护 worker 关闭失败，继续释放数据库资源")

    event_hook_worker = getattr(app.state, "event_hook_worker", None)
    if event_hook_worker is not None:
        try:
            await asyncio.wait_for(event_hook_worker.stop(), timeout=5.0)
        except TimeoutError:
            logger.error("事件 Hook worker 关闭超时，继续释放数据库资源")
        except Exception:
            logger.exception("事件 Hook worker 关闭失败，继续释放数据库资源")

    logger.info("应用生命周期关闭，释放数据库连接")
    container.database().close()
    logger.info("应用生命周期关闭完成")


app = FastAPI(
    title="AETP 自动化设备测试平台 Master API",
    version="0.1.0",
    lifespan=lifespan,
)
register_application_error_handlers(app)


class RequestLoggingMiddleware:
    """纯 ASGI 请求日志中间件。

    不使用 ``@app.middleware('http')`` 的 BaseHTTPMiddleware，避免 SSE
    长连接在 Uvicorn 关闭时通过 AnyIO memory stream 产生额外的
    ``CancelledError`` ASGI 异常栈。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started_at = time.perf_counter()
        status_code = 500

        async def logging_send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, logging_send)
        except asyncio.CancelledError:
            # Uvicorn 在 graceful shutdown 超时后取消长连接，这是预期的
            # 关闭路径，不按应用异常记录堆栈。
            logger.debug("HTTP 请求因服务关闭取消: %s %s", scope["method"], scope["path"])
            raise
        except Exception:
            logger.exception(
                "HTTP 请求异常: %s %s (%.2f ms)",
                scope["method"],
                scope["path"],
                (time.perf_counter() - started_at) * 1000,
            )
            raise
        else:
            logger.info(
                "HTTP 请求完成: %s %s -> %s (%.2f ms)",
                scope["method"],
                scope["path"],
                status_code,
                (time.perf_counter() - started_at) * 1000,
            )


app.add_middleware(RequestLoggingMiddleware)

app.include_router(v1_router)
app.include_router(v2_router)
app.include_router(v2_nodes_router)
app.include_router(v2_tasks_router)


@app.get("/api/v1/health", tags=["system"])
def health(db: DbDep) -> dict[str, str]:
    """健康检查：探活数据库连接。"""
    try:
        with db.session_scope() as session:
            session.execute(sa_text("SELECT 1"))
    except Exception:
        logger.exception("健康检查失败：数据库不可用")
        return {"status": "degraded", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}


@app.get("/api/v1/health/system", tags=["system"])
def system_health(request: Request) -> dict[str, object]:
    """返回面向运维与前端的全局组件状态。"""
    container: Container | None = getattr(request.app.state, "container", None)
    if container is None:
        return {"status": "degraded", "components": {"database": "unavailable"}}

    components = {"database": "ok"}
    try:
        with container.database().session_scope() as session:
            session.execute(sa_text("SELECT 1"))
    except Exception:
        logger.exception("系统健康检查数据库不可用")
        components["database"] = "unavailable"

    mqtt_runtime = getattr(request.app.state, "mqtt_runtime", None)
    settings = get_settings()
    if not settings.mqtt_host:
        components["mqtt"] = "disabled"
    else:
        transport = getattr(mqtt_runtime, "_transport", None)
        components["mqtt"] = "ok" if getattr(transport, "connected", False) else "disconnected"

    maintenance_worker = getattr(request.app.state, "maintenance_worker", None)
    worker_task = getattr(maintenance_worker, "_task", None)
    components["maintenance"] = "ok" if worker_task is not None and not worker_task.done() else "stopped"

    status_value = (
        "degraded"
        if any(value != "ok" for key, value in components.items() if key in {"database", "mqtt", "maintenance"})
        else "ok"
    )
    return {
        "status": status_value,
        "components": components,
        "checked_at": datetime.now(UTC).isoformat(),
    }


# 如果 web/dist 存在，在 API 路由之后挂载前端静态文件
# html=True 表示 SPA 找不到文件时回退到 index.html（vue-router hash history）
web_root = _web_dist_dir()
if web_root is not None:
    app.mount("/", StaticFiles(directory=str(web_root), html=True), name="static")
    logger.info("前端静态文件已挂载: %s", web_root)
else:
    logger.info("未发现前端构建产物，跳过静态文件挂载（仅提供 API）")
