"""主模块侧的从模块注册表：心跳 upsert、在线判定与状态历史。

内存态（主模块重启即清空，从模块下个心跳周期自然重新上报，见设计稿 §7）。
时间基准一律用**主模块接收时刻**：超时判定走**单调钟**（``clock``，缺省
:func:`time.monotonic`，不受 NTP 校时 / 改钟跳变影响），展示时间戳走
**墙钟**（``wall``，缺省 :func:`time.time`）；从模块的 ``sent_at`` 仅参考，
不参与任何判定。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from slave_plugins.heartbeat.capabilities import (
    Capability,
    capabilities_digest,
    parse_capability,
    select_capabilities,
)

from .config import HeartbeatServerConfig

#: 状态转换广播事件（设计稿 §3.2）：``{"node_id", "from", "to", "at"}``
NODE_STATUS_EVENT = "heartbeat/node-status"

#: 节点标识长度上限（与 plugin.json 的 id 同量级）
MAX_NODE_ID: int = 64
#: 节点标识禁止的字符：路径分隔与控制字符（node_id 会出现在 URL 路径里）
_FORBIDDEN_NODE_ID: frozenset[str] = frozenset({"/", "\\", "#", "?", "%", "\r", "\n", "\t"})


class NodeStatus(StrEnum):
    """从模块在线状态（最后心跳时间 + 超时阈值判定，见设计稿 §3.1）。"""

    ONLINE = "online"
    OFFLINE = "offline"
    LOST = "lost"


class HeartbeatServerError(ValueError):
    """心跳协议 / 查询接口的业务错误（映射 HTTP 状态码与错误码）。"""

    def __init__(
        self, message: str, status_code: int = 400, *, code: str = "bad_request"
    ) -> None:
        super().__init__(message)
        self.status_code: int = status_code
        self.code: str = code


@dataclass
class NodeRecord:
    """从模块记录（设计稿 §3 的 ``NodeRecord``）。"""

    node_id: str
    node_kind: str = ""
    address: str = ""
    seq: int = 0
    caps_digest: str = ""
    caps: list[Capability] = field(default_factory=list[Capability])
    #: 不合信封的能力条目（**不静默丢弃**，管理页原样展示）；元素为原始 JSON 值
    unparsed: list[object] = field(default_factory=list[object])
    first_seen: float = 0.0
    last_seen: float = 0.0
    #: 最后一次收到心跳的**单调钟**时刻（超时判定专用，不随墙钟跳变漂移）
    last_beat: float = 0.0
    #: 错过的心跳周期数；**进入 lost 后冻结**（失联前连丢的拍数），心跳到达归零
    missed: int = 0
    status: NodeStatus = NodeStatus.ONLINE

    def to_dict(self) -> dict[str, object]:
        """导出列表视图（能力只给数量，明细走详情接口）。"""
        return {
            "node_id": self.node_id,
            "node_kind": self.node_kind,
            "address": self.address,
            "seq": self.seq,
            "caps_digest": self.caps_digest,
            "status": str(self.status),
            "missed": self.missed,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "caps_count": len(self.caps),
            "unparsed_count": len(self.unparsed),
        }


class NodeRegistry:
    """从模块注册表：upsert 心跳、定时扫描推进状态、维护状态历史。

    参数:
        config: 判定阈值与下发参数。
        emit: 事件出口（``emit(事件名, 载荷)``），状态转换时广播
            :data:`NODE_STATUS_EVENT`；缺省不广播。
        log: 告警出口（如能力条目重复），缺省不记录。
        clock: **单调钟**时间源（超时判定用；测试注入用），缺省
            :func:`time.monotonic`。
        wall: **墙钟**时间源（展示时间戳用；测试注入用），缺省
            :func:`time.time`。
    """

    def __init__(
        self,
        config: HeartbeatServerConfig,
        *,
        emit: Callable[[str, dict[str, object]], None] | None = None,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.config: HeartbeatServerConfig = config
        self._emit = emit
        self._log = log
        self._clock = clock
        self._wall = wall
        self._nodes: dict[str, NodeRecord] = {}
        self._history: deque[dict[str, object]] = deque(maxlen=config.history_size)
        #: seq 回退跟踪：``node_id → (最近回退到的 seq, 低于基线的连续递增拍数)``，
        #: 用于识别「首拍丢失」的重启（见 :meth:`_track_seq_regression`）
        self._regress: dict[str, tuple[int, int]] = {}

    # -- 心跳接收 -------------------------------------------------------------
    def accept(self, payload: Mapping[str, object]) -> tuple[NodeRecord, bool]:
        """处理一次心跳上报（upsert，设计稿 §2 / §7）。

        返回 ``(记录, 是否请求全量能力)``；``node_id`` / ``seq`` 不合法抛
        :class:`HeartbeatServerError`（400）。
        """
        node_id = _require_node_id(payload.get("node_id"))
        beat_at = self._clock()  # 单调钟：超时判定（不受墙钟跳变影响）
        now = self._wall()  # 墙钟：展示时间戳
        record = self._nodes.get(node_id)
        is_new = record is None
        if record is None:
            record = NodeRecord(node_id=node_id, first_seen=now)
            self._nodes[node_id] = record
        record.last_seen = now
        record.last_beat = beat_at
        record.missed = 0
        record.node_kind = _text(payload.get("node_kind"), record.node_kind)

        # 区分「未上报」（键缺失 / 非字符串，保持旧值）与「主动清空」（空串
        # = 清除旧地址）：空串是合法值，不能再用真值判断挡住清除
        raw_address = payload.get("address")
        address = raw_address if isinstance(raw_address, str) else None
        if address is not None and address != record.address:
            if record.address:
                self._record_history(
                    record, "address", **{"from": record.address, "to": address}
                )
            record.address = address

        seq = _require_seq(payload.get("seq"))
        if is_new:
            record.seq = seq
        elif seq < record.seq:
            self._track_seq_regression(record, seq)
        elif seq > record.seq:
            record.seq = seq
            self._regress.pop(node_id, None)  # 旧运行仍在继续：回退候选作废
        # seq 相同（重复心跳）只刷新 last_seen，不推进 seq（不惩罚网络重传）

        payload_digest = _text(payload.get("caps_digest"), "")
        raw_caps = payload.get("capabilities")
        if isinstance(raw_caps, list):
            self._archive_caps(record, cast(list[object], raw_caps))
        need_caps = record.caps_digest != payload_digest

        self._transition(record, NodeStatus.ONLINE)
        return record, need_caps

    def _track_seq_regression(self, record: NodeRecord, seq: int) -> None:
        """seq 回退：判定「从模块重启」还是「乱序迟到」（协议 §2 seq 语义）。

        * ``seq == 1``：发送侧每拍递增、重启后必然从 1 起步 —— 强信号，立即判重启；
        * 其他回退：要求连续两拍构成**低于基线的递增运行**才判重启 —— 从模块
          重启而首拍丢失时，新进程的 seq 会持续低于旧基线并递增；单个乱序
          迟到的旧包不会连续递增，不触发重启（不惩罚网络重传）。
        """
        if seq == 1:
            self._commit_restart(record, seq)
            return
        last, streak = self._regress.get(record.node_id, (seq, 0))
        streak = streak + 1 if seq > last else 1
        self._regress[record.node_id] = (seq, streak)
        if streak >= 2:
            self._commit_restart(record, seq)

    def _commit_restart(self, record: NodeRecord, seq: int) -> None:
        """确认重启：记事件、基线跟随新进程（保留 first_seen，不丢历史）。"""
        self._record_history(
            record,
            "restart",
            message=f"seq 从 {record.seq} 回退到 {seq}（从模块重启）",
        )
        record.seq = seq
        self._regress.pop(record.node_id, None)

    def _archive_caps(self, record: NodeRecord, raw_entries: list[object]) -> None:
        """存档全量能力（摘要变化后从模块下一拍带上，见协议 §2.1）。"""
        caps: list[Capability] = []
        unparsed: list[object] = []
        positions: dict[tuple[str, str], int] = {}
        for raw in raw_entries:
            cap = parse_capability(raw)
            if cap is None:
                unparsed.append(raw)
                continue
            key = (cap.kind, cap.id)
            if key in positions:
                # 同一 (kind, id) 后到覆盖，并记日志（设计稿 §2.1）
                if self._log is not None:
                    self._log(f"节点 {record.node_id} 的能力条目重复：{key[0]}/{key[1]}，后到覆盖")
                caps[positions[key]] = cap
                continue
            positions[key] = len(caps)
            caps.append(cap)
        record.caps = caps
        record.unparsed = unparsed
        record.caps_digest = capabilities_digest(
            cast("Iterable[Mapping[str, object]]", raw_entries)
        )

    # -- 在线判定（定时扫描） --------------------------------------------------
    def sweep(self) -> None:
        """按阈值推进全部节点状态（设计稿 §3.1）。

        ``missed``（错过的心跳周期数）**进入 ``lost`` 后冻结**：那一刻的数值
        即失联前连丢的拍数，此后不再累计（心跳到达后由 :meth:`accept` 归零）。
        """
        now = self._clock()
        for record in self._nodes.values():
            if record.status is NodeStatus.LOST:
                continue  # 失联后 missed 冻结；恢复在线后重新计数
            age = now - record.last_beat
            record.missed = max(0, int(age // self.config.interval))
            if age <= self.config.offline_timeout:
                target = NodeStatus.ONLINE
            elif age <= self.config.lost_timeout:
                target = NodeStatus.OFFLINE
            else:
                target = NodeStatus.LOST
            self._transition(record, target)

    def _transition(self, record: NodeRecord, target: NodeStatus) -> None:
        previous = record.status
        if previous is target:
            return
        record.status = target
        self._record_history(
            record, "status", **{"from": str(previous), "to": str(target)}
        )
        if self._emit is not None:
            self._emit(
                NODE_STATUS_EVENT,
                {
                    "node_id": record.node_id,
                    "from": str(previous),
                    "to": str(target),
                    "at": self._wall(),
                },
            )

    def _record_history(self, record: NodeRecord, kind: str, **fields: object) -> None:
        entry: dict[str, object] = {
            "at": self._wall(),
            "node_id": record.node_id,
            "kind": kind,
        }
        entry.update(fields)
        self._history.append(entry)

    # -- 查询与操作 -------------------------------------------------------------
    def nodes(self) -> list[NodeRecord]:
        """全部节点记录（按 node_id 排序，列表视图稳定）。"""
        return [self._nodes[key] for key in sorted(self._nodes)]

    def get(self, node_id: str) -> NodeRecord | None:
        """按 id 取记录；不存在返回 ``None``。"""
        return self._nodes.get(node_id)

    def history(self, node_id: str | None = None) -> list[dict[str, object]]:
        """状态历史（新在前）；给 id 只看该节点。"""
        entries = [
            entry
            for entry in self._history
            if node_id is None or entry.get("node_id") == node_id
        ]
        return list(reversed(entries))

    def capabilities(
        self,
        *,
        kinds: Collection[str] | None = None,
        labels: Mapping[str, str] | None = None,
        q: str | None = None,
    ) -> list[dict[str, object]]:
        """跨节点能力查询（``select_capabilities`` 的包装）：条目附带 node_id。"""
        result: list[dict[str, object]] = []
        for record in self.nodes():
            for cap in select_capabilities(record.caps, kinds=kinds, labels=labels, q=q):
                entry = cap.to_dict()
                entry["node_id"] = record.node_id
                result.append(entry)
        return result

    def remove(self, node_id: str) -> bool:
        """移除节点记录（含其历史）；不存在返回 ``False``。"""
        if self._nodes.pop(node_id, None) is None:
            return False
        self._regress.pop(node_id, None)
        self._history = deque(
            (entry for entry in self._history if entry.get("node_id") != node_id),
            maxlen=self.config.history_size,
        )
        return True


# -- 载荷校验（协议 §2） -------------------------------------------------------
def _require_node_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HeartbeatServerError("node_id 必填（非空字符串）")
    text = value.strip()
    if len(text) > MAX_NODE_ID or any(
        ch in _FORBIDDEN_NODE_ID or ord(ch) < 32 for ch in text
    ):
        raise HeartbeatServerError(
            f"node_id 不合法（≤{MAX_NODE_ID} 字符，且不能含路径分隔 / 控制字符）"
        )
    return text


def _require_seq(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HeartbeatServerError("seq 需是非负整数")
    return value


def _text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) else fallback


__all__: list[str] = [
    "MAX_NODE_ID",
    "NODE_STATUS_EVENT",
    "HeartbeatServerError",
    "NodeRecord",
    "NodeRegistry",
    "NodeStatus",
]
