"""节点资源占用投影纯函数（§9.8）。

`resource_occupancy` 是心跳上报的展示投影（device_id -> 占用它的 run_id），
调度权威仍是 ``Device.status=BUSY``。它由派发/释放/恢复等多个服务在同一
UoW 事务内维护，因此把「如何增减映射」收敛为纯函数，保证跨服务一致：

- 同一节点可被多个 Run 的多个 Shard 并发占用（按 device 维度聚合）；
- 释放时**只清理当前 Run 的条目**，绝不误删其他 Run 的投影；
- 派发时**覆盖写**（同 device 新 Run 覆盖旧值，配合 failover/重派）。

函数不触碰仓储与事务，仅做映射计算，由调用方负责读 node 并 ``save``。
"""

from __future__ import annotations

from collections.abc import Iterable


def claim_occupancy(
    occupancy: dict,
    *,
    run_id: str,
    device_ids: Iterable[str],
) -> dict:
    """派发成功时，把 device_id -> run_id 写入占用投影（覆盖同 device 旧值）。"""
    device_list = list(device_ids)
    if not device_list:
        return occupancy
    updated = dict(occupancy)
    for device_id in device_list:
        updated[device_id] = run_id
    return updated


def release_occupancy(
    occupancy: dict,
    *,
    run_id: str,
    device_ids: Iterable[str],
) -> dict:
    """Run 终态/取消/离线恢复时，只清理当前 Run 的占用条目。"""
    device_list = list(device_ids)
    if not device_list:
        return occupancy
    updated = dict(occupancy)
    for device_id in device_list:
        if updated.get(device_id) == run_id:
            del updated[device_id]
    return updated


def release_occupancy_for_node(
    node,
    *,
    run_id: str,
    device_ids: Iterable[str],
) -> bool:
    """就地更新 node.resource_occupancy，返回是否发生变更。

    这是 release_occupancy 的薄封装：调用方传入已加载的 node 领域对象，
    变更后仍需自行 ``uow.nodes.save(node)``。
    """
    updated = release_occupancy(node.resource_occupancy, run_id=run_id, device_ids=device_ids)
    if updated == node.resource_occupancy:
        return False
    node.resource_occupancy = updated
    return True
