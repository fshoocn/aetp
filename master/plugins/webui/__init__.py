"""webui 包：可拆卸的 UI 宿主插件（叠在 webapi 之上的展示层）。

对外只暴露三样东西::

    from master.plugins.webui import WebUiPlugin, WebUiConfig, UiContribution

* :class:`WebUiPlugin`  —— 插件本体（也是 ``ctx.webui`` 服务），
  ``ctx.plugin(WebUiPlugin, {...})`` 加载它；
* :class:`WebUiConfig`  —— 配置模型（``ui_directory`` 指向 Vue 构建产物）；
* :class:`UiContribution` —— 插件 UI contribution 数据结构。

与 :mod:`master.plugins.webapi` 的分工：

* **webapi**：服务器、``ctx.webapi``、业务 API 路由 —— 永远需要；
* **webui**：``/ui-static``、SPA 回退、``ctx.webui.register_ui``、``/api/web/ui`` ——
  只在需要展示页面时加载；不加载即为纯 API（无头）部署。

模块结构::

    config.py    配置模型（标准 Schema）
    types.py     纯数据结构（UiContribution）
    plugin.py    WebUiPlugin（UI 宿主服务，provide = "webui"）
    frontend/    Sakai Vue 前端源码（npm run build 产出到 static/ui/dist）
    static/ui/dist/  Vue 构建产物
"""

from .config import WebUiConfig
from .plugin import WebUiPlugin
from .types import UiContribution, UiFormat, UiKind

__all__: list[str] = [
    "UiContribution",
    "UiFormat",
    "UiKind",
    "WebUiConfig",
    "WebUiPlugin",
]
