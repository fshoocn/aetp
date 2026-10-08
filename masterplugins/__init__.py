"""主节点专属的插件源文件目录（未安装）。

例如 :mod:`masterplugins.webui`（Web UI 宿主）。这里的包在主节点上经
``ctx.plugins.install_source`` 安装到 ``master/plugins/`` 后启用——
不安装即为纯 API（无头）部署。从节点专属的源放 ``slaveplugins/``。
"""
