"""跨插件共享契约（非插件公共层）。

插件铁律：插件之间必须通过 cordis 调用，共享的契约类型放本层
（``common/``），不得形成插件包之间的 import。消息派发元数据
:class:`CommMeta` 由 comm 派发、业务插件（如 log）消费，因此住这里；
``common.plugins.comm`` 对外再导出，业务侧两种导入路径等价。
"""

from __future__ import annotations

from typing import TypedDict


class CommMeta(TypedDict):
    """comm 派发给 topic 处理器的元数据。"""

    node: str
    topic: str
    ts: float
    received_ts: float
    id: str


__all__: list[str] = ["CommMeta"]
