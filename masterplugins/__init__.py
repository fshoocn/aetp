"""主节点专属的插件源文件目录（未安装）。

webui 是主节点**内嵌必备**插件，例外随节点目录分发（``master/webui/``）；
其余主节点专属插件源放这里（如未来的通信插件）——默认**非预装**，经
web API 上传安装，列入 ``config.ini`` 的 ``plugins`` 即随启动预装。
安装产物落到 ``master/plugins/``；从节点专属的源放 ``slaveplugins/``。
"""
