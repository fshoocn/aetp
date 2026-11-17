"""跨插件共享契约（非插件公共层）。

插件铁律：插件之间必须通过 cordis 调用，共享的契约类型放本层
（``common/``），不得形成插件包之间的 import。消息派发元数据
:class:`CommMeta` 由 comm 派发、业务插件（如 log）消费，因此住这里；
``common.plugins.comm`` 对外再导出，业务侧两种导入路径等价。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Protocol, TypedDict


class CommMeta(TypedDict):
    """comm 派发给 topic 处理器的元数据。"""

    node: str
    topic: str
    ts: float
    received_ts: float
    id: str


class NodeInfo(TypedDict):
    """节点概要（``ctx.nodes.list()`` 的条目，GAP-3）。"""

    node_id: str
    node_kind: str
    address: str
    status: str


class NodeCapability(TypedDict, total=False):
    """跨节点能力条目：能力信封字段（见 :mod:`common.capabilities`）+ node_id。"""

    node_id: str
    kind: str
    id: str
    provider: str
    version: str
    labels: dict[str, str]
    summary: str
    detail: dict[str, object]


class NodesInterface(Protocol):
    """节点目录服务（``ctx.nodes``）：引擎按能力选节点用（GAP-3）。

    由主模块心跳插件（``heartbeat-server``）提供并包装 ``NodeRegistry``；
    引擎对它是**可选依赖**（从节点没有 nodes 服务，0.1.8 起 optional 声明、
    访问判空）。
    """

    def list(self, *, online_only: bool = False) -> list[NodeInfo]:
        """节点概要列表（``online_only=True`` 只返回在线节点）。"""
        ...

    def select(
        self,
        *,
        kinds: Collection[str] | None = None,
        labels: Mapping[str, str] | None = None,
        online_only: bool = True,
    ) -> list[NodeCapability]:
        """跨节点能力查询（能力信封子集匹配），条目带 ``node_id``。"""
        ...


class UiContributionLike(Protocol):
    """UI 贡献的只读面（webui 的 ``UiContribution`` 结构上满足）。

    只声明清单消费需要的字段；属性用只读 ``property`` 保持协变
    （实现方字段类型是本接口的子类型即可，如 ``kind`` 的字面量）。
    """

    @property
    def id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def owner(self) -> str: ...

    @property
    def path(self) -> str | None: ...

    @property
    def title(self) -> str | None: ...


class UiCatalogInterface(Protocol):
    """UI 清单服务（``ctx.webui``）的消费面：枚举全部前端贡献。

    webapi 的 ``mounts()`` 按此契约汇集页面清单（权限按「方法 + 链接」
    精细匹配的规则来源）——消费方依赖**本契约**而非 webui 插件包，
    符合插件铁律；webui 的 ``WebUiPlugin`` 显式声明实现本接口。
    """

    def ui_contributions(self) -> Sequence[UiContributionLike]: ...


__all__: list[str] = ["CommMeta", "NodeCapability", "NodeInfo", "NodesInterface", "UiCatalogInterface", "UiContributionLike"]
