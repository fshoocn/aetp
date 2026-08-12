"""FastAPI 应用组装。

由 run.py 启动 uvicorn 加载本模块的 app。
lifespan 中创建依赖注入容器并初始化（建表 + 结构同步），
容器实例注入 app.state，路由经 deps 从容器获取依赖。
"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from master.config import get_settings

from .bootstrap.container import Container
from .api.v1.dependencies import DbDep
from .api.v1.errors import register_application_error_handlers
from .api.v1.router import router as v1_router

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
    app.state.container = container

    # 平台管理员 bootstrap：若 users 表为空且配置了管理员凭据，自动创建首个 admin
    _bootstrap_admin(app)
    logger.info("应用启动准备完成")

    yield

    logger.info("应用生命周期关闭，释放数据库连接")
    container.database().close()
    logger.info("应用生命周期关闭完成")


app = FastAPI(
    title="AETP Master API",
    version="0.1.0",
    lifespan=lifespan,
)
register_application_error_handlers(app)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    """记录每个 HTTP 请求的方法、路径、状态码和耗时。"""
    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.exception(
            "HTTP 请求异常: %s %s (%.2f ms)",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "HTTP 请求完成: %s %s -> %s (%.2f ms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response

app.include_router(v1_router)


@app.get("/api/v1/health", tags=["system"])
def health(db: "DbDep") -> dict[str, str]:
    """健康检查：探活数据库连接。"""
    try:
        from sqlalchemy import text as sa_text

        with db.session_scope() as session:
            session.execute(sa_text("SELECT 1"))
    except Exception:
        logger.exception("健康检查失败：数据库不可用")
        return {"status": "degraded", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}


# 如果 web/dist 存在，在 API 路由之后挂载前端静态文件
# html=True 表示 SPA 找不到文件时回退到 index.html（vue-router hash history）
web_root = _web_dist_dir()
if web_root is not None:
    app.mount("/", StaticFiles(directory=str(web_root), html=True), name="static")
    logger.info("前端静态文件已挂载: %s", web_root)
else:
    logger.info("未发现前端构建产物，跳过静态文件挂载（仅提供 API）")

