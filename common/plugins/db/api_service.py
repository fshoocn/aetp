"""db 的 API 服务（``ctx.db_api``，``provide = "db_api"``）：自检接口。

db 插件的界面层拆成两个服务、依赖各归各的，核心永远不沾 Web 依赖：

* ``ctx.db_api``（本服务）—— API 层：``POST /api/db/self-check``；硬依赖
  ``db`` + ``webapi``，从节点 / 无头部署照常可用；
* ``ctx.db_ui``（:class:`~common.plugins.db.ui_service.DbUiService`）——
  页面层：「数据库」Vue 页面；``inject = ["db", "webui"]`` 注入式挂载
  （自家核心 + UI 宿主），两者就位才装载、换装 / 重载随依赖纪元自动重挂，
  无需守护轮询。

依赖缺席时本服务 PENDING、``ctx.db`` 不受影响——db 核心不需要
``inject = ["webapi"]``，Web 依赖全部圈在界面层服务里。
"""

from __future__ import annotations

import json
from typing import ClassVar, cast

from cordis_port import Context, Service
from starlette.requests import Request

from .service import DbService


class DbApiService(Service[None]):
    """db API 服务：挂 ``POST /api/db/self-check`` 自检接口。"""

    #: 附属服务标记：本类是 ``db`` 插件的界面层载体之一，不是独立插件——
    #: 插件清单（builtin_specs）据此不单列，随宿主插件 db 一起呈现
    service_of: ClassVar[str] = "db"
    #: 服务名（即 ``ctx.db_api``）
    provide: str | None = "db_api"
    #: fiber 名（诊断与贡献归属用）
    name: str = "db_api"
    #: API 层的依赖集（db 核心只有 ``appconfig`` —— 各归各的）
    inject: ClassVar[list[str]] = ["db", "webapi"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.db_api
        self.ctx: Context = ctx

    def init(self) -> None:
        """挂自检接口（依赖集由 fiber 保证就绪，无需轮询）。

        路由注册绑定调用方 fiber：本服务卸载 / 重载时自动撤销重挂。
        """
        api = self.ctx.webapi
        api.post(
            "/api/db/self-check",
            self._self_check,
            kind="api",
            name="db.self-check",
        )

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


__all__: list[str] = ["DbApiService"]
