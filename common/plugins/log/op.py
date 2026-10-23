"""操作关联 id（轻量 trace）——实现已移至公共层 :mod:`common.log_op`。

插件铁律：插件之间必须通过 cordis 调用，共享工具 / 契约放**非插件公共层**；
本模块仅保留再导出，维持 ``common.plugins.log`` 既有公开导入路径
（``log_operation`` / ``log_operation_async`` 供 log 出口与 webapi 管理面使用）。
"""

from common.log_op import current_operation, log_operation, log_operation_async

__all__: list[str] = ["current_operation", "log_operation", "log_operation_async"]
