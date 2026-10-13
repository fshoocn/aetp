"""主从共用的插件源文件目录（未安装）。

存放**主从节点都能使用**的插件源包。其中 :mod:`common.plugins.webapi`
是运行时底座（安装器与服务宿主），由节点入口直接以源码加载；
其余插件包在需要时经 ``ctx.plugins.install_source`` 安装到各节点的
``<节点>/plugins/`` 安装目录后再启用。

目录约定：**预装插件源放节点目录下**（``master/webui``、``slave/heartbeat``
等，随节点分发、列入 config.ini 的 ``plugins`` 即随启动预装）；
``<节点>/plugins/`` 安装目录只放**可选插件**（经 Web 上传 / 分发落地的
安装产物，含 registry 与归档）。
"""
