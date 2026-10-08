"""向 Web UI 暴露插件包上传和生命周期管理 API。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from cordis_port import Context
from starlette.requests import Request
from starlette.responses import JSONResponse

from .plugin_manager import MAX_ARCHIVE_SIZE, PluginManager, PluginManagerError


class PluginManagerPlugin:
    name: ClassVar[str] = "plugin-manager"
    inject: ClassVar[list[str]] = ["web"]

    def __init__(self, ctx: Context, config: dict[str, str | Path]) -> None:
        self.ctx: Context = ctx
        self.install_root: Path = Path(config["install_root"]).resolve()
        self.manager: PluginManager | None = None

    async def init(self) -> None:
        loop = asyncio.get_running_loop()
        self.manager = PluginManager(self.ctx, loop, self.install_root)
        self._register_routes()
        await self.manager.restore_enabled()

    def _register_routes(self) -> None:
        ctx = self.ctx

        def manager() -> PluginManager:
            if self.manager is None:
                raise RuntimeError("插件管理器尚未初始化")
            return self.manager

        def error_response(error: PluginManagerError) -> JSONResponse:
            return ctx.web.json({"error": str(error)}, status_code=error.status_code)

        @ctx.web.get("/api/plugins", kind="api", name="plugins.list")
        def list_plugins(_: Request) -> dict[str, object] | JSONResponse:
            try:
                items = manager().run_from_web_thread(manager().list_plugins())
                return {"plugins": items}
            except PluginManagerError as error:
                return error_response(error)

        @ctx.web.post("/api/plugins/upload", kind="api", name="plugins.upload")
        async def upload_plugin(_request: Request) -> dict[str, object] | JSONResponse:
            try:
                declared_size = _request.headers.get("content-length")
                if declared_size and int(declared_size) > MAX_ARCHIVE_SIZE:
                    raise PluginManagerError("插件 ZIP 最大允许 32 MiB", 413)
                chunks: list[bytes] = []
                total_size = 0
                async for chunk in _request.stream():
                    total_size += len(chunk)
                    if total_size > MAX_ARCHIVE_SIZE:
                        raise PluginManagerError("插件 ZIP 最大允许 32 MiB", 413)
                    chunks.append(chunk)
                item = await asyncio.to_thread(
                    manager().run_from_web_thread,
                    manager().install_archive(b"".join(chunks)),
                )
                return {"plugin": item}
            except PluginManagerError as error:
                return error_response(error)
            except ValueError:
                return ctx.web.json({"error": "Content-Length 无效"}, status_code=400)

        @ctx.web.post("/api/plugins/{plugin_id}/enable", kind="api", name="plugins.enable")
        def enable_plugin(_: Request, plugin_id: str) -> dict[str, object] | JSONResponse:
            try:
                item = manager().run_from_web_thread(manager().enable(plugin_id))
                return {"plugin": item}
            except PluginManagerError as error:
                return error_response(error)

        @ctx.web.post("/api/plugins/{plugin_id}/disable", kind="api", name="plugins.disable")
        def disable_plugin(_: Request, plugin_id: str) -> dict[str, object] | JSONResponse:
            try:
                item = manager().run_from_web_thread(manager().disable(plugin_id))
                return {"plugin": item}
            except PluginManagerError as error:
                return error_response(error)

        @ctx.web.delete("/api/plugins/{plugin_id}", kind="api", name="plugins.uninstall")
        def uninstall_plugin(_: Request, plugin_id: str) -> dict[str, object] | JSONResponse:
            try:
                manager().run_from_web_thread(manager().uninstall(plugin_id))
                return {"ok": True, "id": plugin_id}
            except PluginManagerError as error:
                return error_response(error)


__all__: list[str] = ["PluginManagerPlugin"]