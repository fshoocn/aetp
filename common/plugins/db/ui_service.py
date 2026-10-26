"""db 的界面服务（``ctx.db_ui``，``provide = "db_ui"``）：自检接口 +「数据库」页面。

cordis 里一个插件可以提供多个服务，且**每个服务的依赖集各不相同**——db
插件据此拆成两层，让核心永远不沾 Web 依赖：

* ``ctx.db``（:class:`~common.plugins.db.service.DbService`）——核心数据
  服务：建表 / 查询 / 归属账本 / 自检引擎；只依赖 ``appconfig``，从节点
  与无头部署照常工作；
* ``ctx.db_ui``（本服务）——界面层：``POST /api/db/self-check`` 与
  「数据库」Vue 页面；硬依赖 ``db`` + ``webapi``，与核心互不牵连。

依赖缺席时本服务 PENDING、``ctx.db`` 不受影响——db 核心不需要
``inject = ["webui"]``，界面依赖全部圈在界面服务里。``webui`` 是**可选
依赖**（从节点没有 webui，自检接口仍可用），故不写进 ``inject``，页面走
就绪守护注册（与 log 查看页同款）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar, cast

from cordis_port import Context, Service
from starlette.requests import Request

from .service import DbService


class DbUiService(Service[None]):
    """db 界面服务：挂 ``POST /api/db/self-check``、注册「数据库」页面。"""

    #: 附属服务标记：本类是 ``db`` 插件的第二个服务载体，不是独立插件——
    #: 插件清单（builtin_specs）据此不单列，随宿主插件 db 一起呈现
    service_of: ClassVar[str] = "db"
    #: 服务名（即 ``ctx.db_ui``）
    provide: str | None = "db_ui"
    #: fiber 名（webui 贡献归属由此而来：``db_ui:db``）
    name: str = "db_ui"
    #: 界面服务自己的依赖集（db 核心只有 ``appconfig`` —— 各归各的）
    inject: ClassVar[list[str]] = ["db", "webapi"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.db_ui
        self.ctx: Context = ctx
        self._page_task: asyncio.Task[None] | None = None

    async def init(self) -> Callable[[], Awaitable[None]]:
        """挂自检接口（依赖集由 fiber 保证就绪，无需轮询）并起页面守护。"""
        api = self.ctx.webapi
        api.post(
            "/api/db/self-check",
            self._self_check,
            kind="api",
            name="db.self-check",
        )
        self._page_task = asyncio.create_task(self._page_guard())
        self._page_task.add_done_callback(self._log_task_exit)

        async def cleanup() -> None:
            task, self._page_task = self._page_task, None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        return cleanup

    def _log_task_exit(self, task: asyncio.Task[None]) -> None:
        """后台任务观察哨：意外退出记 error 日志，不再无声消失。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("db 界面服务后台任务意外退出：%s", error)

    async def _page_guard(self) -> None:
        """「数据库」页面守护：webui 就绪即注册，换装 / 重载后自动补注册。

        webui 是可选依赖（从节点没有 webui），不能写进 ``inject``——否则
        从节点整服务起不来、自检接口也没了；故走就绪守护（log 查看页同款）。
        无头部署自然空转。
        """
        while True:
            webui = self.ctx.get("webui")
            if webui is not None:
                known = {item.id for item in webui.ui_contributions()}
                if "db_ui:db" not in known:
                    webui.register_ui(
                        "db",
                        kind="page",
                        format="vue",
                        resource_root=str(Path(__file__).resolve().parent / "ui"),
                        entry="DbPage.vue",
                        path="/db",
                        title="数据库",
                        menu_group="",
                        menu_icon="pi pi-database",
                        menu_order=101,
                    )
            await asyncio.sleep(5.0)

    # --------------------------------------------------------------- 自检接口
    async def _self_check(self, request: Request) -> dict[str, object]:
        """``POST /api/db/self-check``：手动触发数据自检。

        请求体可选 ``{"policy": "warn" | "drop"}``（缺省取 ``[db]
        orphan_policy``）：``warn`` 只报告，``drop`` 清理孤儿。
        """
        db = cast(DbService, self.ctx.get("db"))
        policy = str(db.config.orphan_policy)
        raw = await request.body()
        if raw:
            try:
                parsed: object = json.loads(raw)
            except ValueError:
                return self._error("请求体不是合法 JSON", 400)
            if isinstance(parsed, dict):
                body = cast(dict[str, object], parsed)
                if "policy" in body:
                    policy = str(body.get("policy") or "")
        try:
            return await db.self_check(policy=policy)
        except ValueError as exc:
            return self._error(str(exc), 400)

    def _error(self, message: str, status_code: int) -> dict[str, object]:
        api = self.ctx.get("webapi")
        return api.json({"error": message}, status_code=status_code)


__all__: list[str] = ["DbUiService"]
