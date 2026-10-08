"""主从共用的插件源文件目录（未安装）。

存放**主从节点都能使用**的插件源包。其中 :mod:`common.plugins.webapi`
是运行时底座（安装器与服务宿主），由节点入口直接以源码加载；
其余插件包在需要时经 ``ctx.plugins.install_source`` 安装到各节点的
``<节点>/plugins/`` 安装目录后再启用。仅主节点用的源放 ``masterplugins/``，
仅从节点用的源放 ``slaveplugins/``。
"""
