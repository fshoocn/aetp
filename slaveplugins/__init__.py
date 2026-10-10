"""从节点专属的插件源文件目录（未安装）。

从节点插件源放这里（如未来的节点代理、执行插件）——默认**非预装**，经
``ctx.plugins.install_source`` 安装到 ``slave/plugins/`` 后启用，列入
``config.ini`` 的 ``plugins`` 即随启动预装。主从共用的源放
``common/plugins/``，主节点专属的源放 ``masterplugins/``。
"""
