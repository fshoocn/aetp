"""Web 服务包：提供 API、静态资源和前端 UI contribution 注册。

对外只暴露四样东西::

    from plugins.web import WebPlugin, WebService, UvicornWebService, WebConfig

* :class:`WebPlugin`    —— 插件本体，``ctx.plugin(WebPlugin, {...})`` 加载它；
* :class:`WebService`   —— **接口**，业务插件通过 ``ctx.web`` 依赖的契约；
* :class:`UvicornWebService` —— 接口的 uvicorn + Starlette 实现（默认后端）；
* :class:`WebConfig`    —— 配置模型。

换服务器时，只需要改 :class:`~plugins.web.plugin.WebPlugin` 构造里那一行
``UvicornWebService(ctx, config)``（换成同接口的其他后端），接口与所有业务插件保持不变。

模块结构::

    config.py               配置模型（标准 Schema）
    types.py                纯数据结构（路由记录和 UI contribution）
    interface.py            接口 WebService（换后端时不动）
    uvicorn_web_service.py  uvicorn 后端：UvicornWebService + UvicornHost
    router.py               路由表（登记、包装、随插件卸载）
    responses.py            handler 返回值 → HTTP 响应
    plugin.py               WebPlugin（Web 服务生命周期与管理 API 自动加载）
    plugin_manager.py       插件包安装、启停与持久化
    plugin_manager_plugin.py 插件管理 API
"""

from .config import WebConfig
from .interface import WebService
from .plugin import WebPlugin
from .types import (
    HttpMethod,
    RouteKind,
    RouteRecord,
    StreamContent,
    UiContribution,
    UiFormat,
    UiKind,
)
from .uvicorn_web_service import ServerError, UvicornWebService

__all__: list[str] = [
    "HttpMethod",
    "RouteKind",
    "RouteRecord",
    "ServerError",
    "StreamContent",
    "UiContribution",
    "UiFormat",
    "UiKind",
    "UvicornWebService",
    "WebConfig",
    "WebPlugin",
    "WebService",
]
