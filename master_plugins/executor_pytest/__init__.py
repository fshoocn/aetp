"""pytest 执行器管理端插件包（master_plugins/executor_pytest）。

本包是 executor-pytest-master 插件（plugin.json: id = executor-pytest-master）的
实现，承载材料分块预推 / 缺块补发（:mod:`.material`）、inspect 编排
（:class:`.plugin.ExecPytestMasterPlugin`）与族内管理 API（:mod:`.api_service`），
配置节 ``[executor_pytest_master]``（:mod:`.config`）。材料与用例数据的存储归
test_core（``ctx.test_core``，D10 数据唯一写者），本包不留材料副本；族内协议
（topic / 消息字段）与执行端 ``slave_plugins/executor_pytest`` 逐字对齐
（[slave_plugins/executor_pytest/DESIGN.md](../../slave_plugins/executor_pytest/DESIGN.md)
§5/§6/§13）。

公开导出见 ``__all__``；跨插件共用的材料服务面契约住
:mod:`common.test_contracts`（消费插件请从那里 import，勿经本包）。
"""

from __future__ import annotations

from .config import ExecPytestMasterConfig
from .material import (
    CAP_ID,
    CAP_KIND,
    MATERIAL_WAIT,
    PROTO,
    PUSH_DONE,
    TOPIC_INSPECT,
    TOPIC_INSPECT_RESULT,
    TOPIC_MATERIAL,
    TOPIC_MATERIAL_CHUNK,
    TOPIC_MATERIAL_MISSING,
    MaterialPusher,
    NoCandidateNode,
    PushEntry,
    require_core,
    split_chunks,
)
from .plugin import ExecPytestMasterPlugin, InspectMode, InspectRecord

# 公开面（isort 序；跨插件契约请 import common.test_contracts）
__all__: list[str] = [
    "CAP_ID",
    "CAP_KIND",
    "MATERIAL_WAIT",
    "PROTO",
    "PUSH_DONE",
    "TOPIC_INSPECT",
    "TOPIC_INSPECT_RESULT",
    "TOPIC_MATERIAL",
    "TOPIC_MATERIAL_CHUNK",
    "TOPIC_MATERIAL_MISSING",
    "ExecPytestMasterConfig",
    "ExecPytestMasterPlugin",
    "InspectMode",
    "InspectRecord",
    "MaterialPusher",
    "NoCandidateNode",
    "PushEntry",
    "require_core",
    "split_chunks",
]
