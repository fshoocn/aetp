"""能力信封契约——实现已移至公共层 :mod:`common.capabilities`。

插件铁律：插件之间必须通过 cordis 调用，共享契约放**非插件公共层**；
master / slave 两侧心跳插件同用本契约，本模块仅保留再导出，维持
``slave_plugins.heartbeat.capabilities`` 既有公开导入路径。
"""

from common.capabilities import (
    CAP_EVENT,
    Capability,
    CapProblemAlerter,
    canonical_json,
    capabilities_digest,
    capability,
    collect_capabilities,
    parse_capability,
    select_capabilities,
)

__all__: list[str] = [
    "CAP_EVENT",
    "CapProblemAlerter",
    "Capability",
    "canonical_json",
    "capabilities_digest",
    "capability",
    "collect_capabilities",
    "parse_capability",
    "select_capabilities",
]
