"""webapi 服务包：HTTP API、路由与静态资源（不含 UI 宿主）。

对外只暴露四样东西::

    from common.plugins.webapi import WebApiPlugin, WebApiService, UvicornWebApiService, WebApiConfig

* :class:`WebApiPlugin`    —— 插件本体，依赖 ``appconfig`` 并读取 ``[web]`` 配置；
* :class:`WebApiService`   —— **接口**，业务插件通过 ``ctx.webapi`` 依赖的契约；
* :class:`UvicornWebApiService` —— 接口的 uvicorn + Starlette 实现（默认后端）；
* :class:`WebApiConfig`    —— 配置模型。

换服务器时，只需要改 :class:`~common.plugins.webapi.plugin.WebApiPlugin` 构造里
那一行 ``UvicornWebApiService(ctx, config)``（换成同接口的其他后端），
接口与所有业务插件保持不动。

UI 是可拆卸的上层插件（:mod:`master.webui`，源在 ``master/webui/``）：SPA
静态资源、页面回退与 ``register_ui`` 都在那里；不加载 webui 即为纯 API 部署。

模块结构::

    config.py               配置模型（标准 Schema）
    types.py                纯数据结构（路由记录）
    interface.py            接口 WebApiService（换后端时不动）
    uvicorn_web_service.py  uvicorn 后端：UvicornWebApiService + UvicornHost
    router.py               路由表（登记、包装、随插件卸载）
    responses.py            handler 返回值 → HTTP 响应
    plugin.py               WebApiPlugin（Web 服务生命周期与管理 API 自动加载）
    plugin_manager.py       插件包安装、启停与持久化
    plugin_manager_plugin.py 插件管理 API
"""

from .config import WebApiConfig
from .interface import WebApiService
from .plugin import WebApiPlugin
from .types import (
    HttpMethod,
    RouteKind,
    RouteRecord,
    StreamContent,
)
from .uvicorn_web_service import ServerError, UvicornWebApiService

__all__: list[str] = [
    "HttpMethod",
    "RouteKind",
    "RouteRecord",
    "ServerError",
    "StreamContent",
    "UvicornWebApiService",
    "WebApiConfig",
    "WebApiPlugin",
    "WebApiService",
]
