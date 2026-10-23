"""模型声明 DSL——实现已移至公共层 :mod:`common.models`。

插件铁律：插件之间不得直接 import，共享契约 / DSL 放**非插件公共层**；
数据服务经 ``inject = ["db"]`` + ``ctx.db`` 调用。本模块仅保留再导出，
维持 ``common.plugins.db.models`` 既有公开导入路径。
"""

from common.models import (
    MAX_IDENT,
    TABLE_PREFIX,
    TYPE_TOKENS,
    Mapped,
    Model,
    compact,
    decode_plugin_id,
    mapped_column,
    model_logical,
    model_owner,
    module_owner,
    owner_index_name,
    owner_models,
    physical_name,
    registered_models,
    reset_owner_group,
    slug,
    snake,
    type_token,
)

__all__: list[str] = [
    "MAX_IDENT",
    "TABLE_PREFIX",
    "TYPE_TOKENS",
    "Mapped",
    "Model",
    "compact",
    "decode_plugin_id",
    "mapped_column",
    "model_logical",
    "model_owner",
    "module_owner",
    "owner_index_name",
    "owner_models",
    "physical_name",
    "registered_models",
    "reset_owner_group",
    "slug",
    "snake",
    "type_token",
]
