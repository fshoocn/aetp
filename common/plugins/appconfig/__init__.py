"""appconfig：读取执行根目录 ``config.ini`` 的公共插件（主从共用）。

* :class:`~common.plugins.appconfig.config.AppConfig` —— ``config.ini``
  对应的配置类（节视图 + 类型化取值）；
* :class:`~common.plugins.appconfig.plugin.AppConfigPlugin` —— cordis 集成：
  注册 ``ctx.appconfig`` 服务，供其他插件 ``inject = ["appconfig"]`` 取用；
* 由 :func:`common.node_runtime.assemble_node` 在装配节点时**最先加载**，
  主 / 从节点都可用。
"""

from __future__ import annotations

from .config import AppConfig, ConfigSection
from .plugin import AppConfigPlugin

__all__: list[str] = ["AppConfig", "AppConfigPlugin", "ConfigSection"]
