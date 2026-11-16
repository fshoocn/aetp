"""plugin-manager 的管理 API 服务（``ctx.plugins_api``）：``/api/plugins`` 系列。

插件管理核心（PluginManagerPlugin，``ctx.plugins``）承载安装 / 启停 / 配置与
启动预装，不沾 Web 依赖；管理 HTTP 层独立成本服务、``inject = ["plugins",
"webapi"]`` **注入式挂载**——两者就位 cordis 才装载并挂路由（依赖缺席安静
PENDING，核心 / webapi 换装、重载随依赖纪元自动重挂）。处理器经
``ctx.plugins`` 公共服务面调用，与服务入口（``inject = ["plugins"]``）是
同一套实现。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, ClassVar, cast

from cordis_port import Context, Service
from starlette.requests import Request
from starlette.responses import JSONResponse

from .plugin_manager import MAX_ARCHIVE_SIZE, PluginManagerError

if TYPE_CHECKING:  # 运行期无环：plugin_manager_plugin 导入本模块装载子服务
    from .plugin_manager_plugin import PluginManagerPlugin

#: 配置类接口的请求体上限（与 plugin.json 同量级；其余入口：ZIP 32 MiB、心跳 1 MiB）
MAX_CONFIG_BODY_BYTES: int = 64 * 1024
#: 超限请求的引流额度：拒绝前最多再读这么多字节丢弃，防无限长流挂死
_DRAIN_SLACK: int = 256 * 1024


async def _read_body_capped(request: Request, *, limit: int, message: str) -> bytes:
    """读请求体（超限抛 413）。

    超限不立刻中断读取，而是继续**引流丢弃**（最多再读 :data:`_DRAIN_SLACK`
    字节）再拒绝：请求体读完后连接才能优雅收尾，否则服务端带未读数据关
    连接触发 RST，客户端连拒绝响应都读不到。
    """
    chunks: list[bytes] = []
    total = 0
    overflow = False
    async for chunk in request.stream():
        total += len(chunk)
        if overflow or total > limit:
            overflow = True
            chunks.clear()
            if total > limit + _DRAIN_SLACK:
                break  # 引流额度用完：不再陪恶意长流耗着
            continue
        chunks.append(chunk)
    if overflow:
        raise PluginManagerError(message, 413)
    return b"".join(chunks)


class PluginManagerApiService(Service[None]):
    """plugin-manager 管理 API 服务：挂 ``/api/plugins`` 系列接口。"""

    #: 附属服务标记：本类是 ``plugin-manager`` 的界面层载体之一，不是独立插件——
    #: 插件清单（builtin_specs）据此不单列，随宿主 plugin-manager 一起呈现
    service_of: ClassVar[str] = "plugin-manager"
    #: 服务名（即 ``ctx.plugins_api``）
    provide: str | None = "plugins_api"
    #: fiber 名（路由归属与诊断用）
    name: str = "plugins_api"
    #: 管理 API 的依赖集 = 自家核心 ``plugins`` + Web 服务 ``webapi``：两者
    #: 就位才装载，依赖缺席安静 PENDING（核心因此不沾 webapi 依赖）
    inject: ClassVar[list[str]] = ["plugins", "webapi"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.plugins_api
        self.ctx: Context = ctx

    def init(self) -> None:
        """挂管理接口（依赖集由 fiber 保证就绪，无需轮询）。

        路由注册绑定调用方 fiber：本服务卸载 / 重载时自动撤销重挂。
        """
        api = self.ctx.webapi
        api.get("/api/plugins", self._list, kind="api", name="plugins.list")
        api.post("/api/plugins/upload", self._upload, kind="api", name="plugins.upload")
        api.post(
            "/api/plugins/{plugin_id}/enable", self._enable, kind="api", name="plugins.enable"
        )
        api.post(
            "/api/plugins/{plugin_id}/disable", self._disable, kind="api", name="plugins.disable"
        )
        api.delete(
            "/api/plugins/{plugin_id}", self._uninstall, kind="api", name="plugins.uninstall"
        )
        api.patch(
            "/api/plugins/{plugin_id}/config",
            self._update_config,
            kind="api",
            name="plugins.update_config",
        )
        api.get(
            "/api/plugins/{plugin_id}/readme", self._readme, kind="api", name="plugins.readme"
        )

    @property
    def _plugins(self) -> PluginManagerPlugin:
        return cast("PluginManagerPlugin", self.ctx.get("plugins"))

    def _error(self, error: PluginManagerError) -> JSONResponse:
        payload: dict[str, object] = {"error": str(error)}
        if error.details:
            payload["details"] = error.details
        return self.ctx.webapi.json(payload, status_code=error.status_code)

    async def _list(self, _: Request) -> dict[str, object] | JSONResponse:
        try:
            items = await self._plugins.list_plugins()
            return {"plugins": items}
        except PluginManagerError as error:
            return self._error(error)

    async def _upload(self, request: Request) -> dict[str, object] | JSONResponse:
        try:
            archive = await _read_body_capped(
                request, limit=MAX_ARCHIVE_SIZE, message="插件 ZIP 最大允许 32 MiB"
            )
            # replace=1 是操作者在确认弹窗后给出的显式替换确认
            replace = request.query_params.get("replace", "") in {"1", "true", "yes"}
            item = await self._plugins.install_archive(archive, replace=replace)
            # 新装 201 Created；替换（含幂等重放）200 OK
            return self.ctx.webapi.json(
                {"plugin": item}, status_code=201 if not replace else 200
            )
        except PluginManagerError as error:
            return self._error(error)

    async def _enable(self, _: Request, plugin_id: str) -> dict[str, object] | JSONResponse:
        try:
            return {"plugin": await self._plugins.enable(plugin_id)}
        except PluginManagerError as error:
            return self._error(error)

    async def _disable(self, _: Request, plugin_id: str) -> dict[str, object] | JSONResponse:
        try:
            return {"plugin": await self._plugins.disable(plugin_id)}
        except PluginManagerError as error:
            return self._error(error)

    async def _uninstall(
        self, request: Request, plugin_id: str
    ) -> dict[str, object] | JSONResponse:
        try:
            # keep_data=1 保留数据表；drop=1 强制删除被依赖插件的数据
            keep_data = request.query_params.get("keep_data", "") in {"1", "true", "yes"}
            drop = request.query_params.get("drop", "") in {"1", "true", "yes"}
            await self._plugins.uninstall(plugin_id, keep_data=keep_data, drop=drop)
            return {"ok": True, "id": plugin_id}
        except PluginManagerError as error:
            return self._error(error)

    async def _update_config(
        self, request: Request, plugin_id: str
    ) -> dict[str, object] | JSONResponse:
        """部分更新插件配置（请求体 JSON 对象 = 要改的键值，其余键保留）。"""
        try:
            raw_body = await _read_body_capped(
                request, limit=MAX_CONFIG_BODY_BYTES, message="配置请求体最大允许 64 KiB"
            )
            try:
                body: object = json.loads(raw_body)
            except (ValueError, RecursionError) as error:
                raise PluginManagerError(f"请求体不是合法 JSON：{error}", 400) from error
            if not isinstance(body, dict):
                raise PluginManagerError("请求体必须是 JSON 对象", 400)
            item = await self._plugins.update_config(plugin_id, cast(dict[str, object], body))
            return {"plugin": item}
        except PluginManagerError as error:
            return self._error(error)

    async def _readme(self, _: Request, plugin_id: str) -> dict[str, object] | JSONResponse:
        """插件描述文件（Markdown 文本），供管理页弹窗展示。"""
        try:
            content = await self._plugins.plugin_readme(plugin_id)
            return {"id": plugin_id, "content": content}
        except PluginManagerError as error:
            return self._error(error)


__all__: list[str] = ["MAX_CONFIG_BODY_BYTES", "PluginManagerApiService"]
