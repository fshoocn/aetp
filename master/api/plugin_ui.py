"""Web 插件 UI 静态托管 API。

Web Shell 以同源 iframe 加载已启用插件（不限 point）归档内携带的 ``ui/`` 页面。
请求格式：

    /plugins/{plugin_id}/{version}/ui            -> 默认文档（entrypoints.ui）
    /plugins/{plugin_id}/{version}/ui/{path}     -> ui/ 目录内静态资源

只服务 ENABLED 且 Manifest 声明了 ``entrypoints.ui`` 的插件（executor 等任务插件
可携带配置/上传/生成用例界面）；路径在 ``PluginUiHost`` 内做越界防护。静态页面不
含平台敏感数据，与 Web dist 一样公开可读；页面与宿主之间的结构化消息由 Web 侧
postMessage 协议处理（规范 §6.3）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from master.adapters.plugin_ui.host import PluginUiHost
from master.api.dependencies import get_plugin_ui_host

router = APIRouter(prefix="/plugins", tags=["plugin-ui"])

PluginUiHostDep = Annotated[PluginUiHost, Depends(get_plugin_ui_host)]


def _not_found(detail: str = "插件 UI 资源不存在") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


@router.get("/{plugin_id}/{version}/ui")
def serve_plugin_ui_root(
    plugin_id: str,
    version: str,
    request: Request,
    host: PluginUiHostDep,
) -> FileResponse:
    """返回 UI 插件 Manifest 声明的默认文档（entrypoints.ui）。"""
    del request
    asset = host.resolve(plugin_id, version, None)
    if asset is None:
        raise _not_found()
    return FileResponse(asset.path, media_type=asset.media_type)


@router.get("/{plugin_id}/{version}/ui/{file_path:path}")
def serve_plugin_ui_file(
    plugin_id: str,
    version: str,
    file_path: str,
    request: Request,
    host: PluginUiHostDep,
) -> FileResponse:
    """返回 ui/ 目录内的静态资源（JS/CSS/图片/子页面等）。"""
    del request
    asset = host.resolve(plugin_id, version, file_path)
    if asset is None:
        raise _not_found()
    return FileResponse(asset.path, media_type=asset.media_type)
