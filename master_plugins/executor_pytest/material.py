"""材料分块推送器（executor_pytest DESIGN §5）：自描述分块 / 预推 / 缺块补发。

* **自描述分块**（逐字对齐执行端收件解析）：元包 ``pytest.material``
  ``{proto, material_id, sha256, recipe_fingerprint, total, chunk_size, recipe,
  selections?}`` 与分块 ``pytest.material.chunk``
  ``{proto, material_id, sha256, total, index, data_b64}``——每条 chunk 原文
  ``chunk_size``（默认 64KiB，b64 后约 87KiB），元信息冗余进每条 chunk，
  跨 topic 乱序安全（chunk 不等元包先行）；
* **selections 随预推同步**（§20-Q1 定案 (a)）：``[{selection_id, case_ids}]``
  进元包，执行端落 ``{cache}/{material_id}/selections/{selection_id}.json``；
* **预推**（§5.2 主路径）：候选 = ``executor/pytest`` 能力节点，``push_concurrency``
  并发限流，对每节点按序发元包 + chunk（AT_LEAST_ONCE）；材料 zip 本体经
  ``ctx.test_core.get_material_bytes`` 读取，**本插件不留副本**；
* **缺块补发**：收 ``pytest.material.missing``
  ``{proto, material_id, node_id, missing: [index…]}`` 只补缺块（空清单 =
  从端 total 未知，整包补推），补发幂等（执行端按 ``(material_id, index)`` 去重）；
* **就绪跟踪**：每 (node, material) 记 ``pending`` 缺块集（发出即计 pending），
  ``material_wait`` 状态供 stats（预推在途数）。

族内 topic / 载荷字段与执行端 ``slave_plugins/executor_pytest/plugin.py``
逐字一致（proto=1，不认识版本拒收，D8）。
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from cordis_port import Context

from common.test_contracts import MaterialNotFound, TestCoreError, TestCoreInterface

# -- 族内私有 topic 与协议版本（与执行端同值，本包自管不进契约） -------------

#: 族内私有消息的协议版本（不认识版本拒收，D8）
PROTO = 1
#: 主→从：材料元包（recipe / selection 清单；chunk 不等它）
TOPIC_MATERIAL = "pytest.material"
#: 主→从：自描述材料分块（每条含 total / sha256，跨 topic 乱序安全）
TOPIC_MATERIAL_CHUNK = "pytest.material.chunk"
#: 从→主：缺块补发请求（missing = 坏 / 缺 index 清单）
TOPIC_MATERIAL_MISSING = "pytest.material.missing"
#: 主→从：脚本分析 / 用例解析请求（定向单节点）
TOPIC_INSPECT = "pytest.inspect"
#: 从→主：分析 / 解析应答
TOPIC_INSPECT_RESULT = "pytest.inspect.result"

#: at-least-once 投递策略（comm DeliveryPolicy 字面量，不 import comm 包）
_AT_LEAST_ONCE: dict[str, int] = {"min_success": 1}

#: 执行器能力信封过滤口径（DESIGN §6.1：候选 = ``executor/pytest`` 能力节点）
CAP_KIND = "executor"
CAP_ID = "pytest"

#: 就绪跟踪状态：预推在途 / 缺块补发中（发出即计 pending）
MATERIAL_WAIT = "material_wait"
#: 就绪跟踪状态：预推完成（补发仍按 pending 清单幂等进行）
PUSH_DONE = "pushed"


class NoCandidateNode(TestCoreError, ValueError):
    """无可用候选执行节点（TestCoreError 族 + ValueError 口径；API 层映射 503）。"""


def require_core(ctx: Context) -> TestCoreInterface:
    """解析 ``ctx.test_core`` 服务（缺席抛 :class:`TestCoreError`）。"""
    service = ctx.get("test_core")
    if service is None:
        raise TestCoreError("test_core 服务未就绪")
    return cast(TestCoreInterface, service)


def split_chunks(data: bytes, chunk_size: int) -> list[bytes]:
    """材料 zip 按 ``chunk_size`` 切块（0 / 负值钳到 1，防退化死循环）。

    空材料回单个空块（``total ≥ 1``，执行端 ``total <= 0`` 拒收）。
    """
    size = max(1, int(chunk_size))
    if not data:
        return [b""]
    return [data[index : index + size] for index in range(0, len(data), size)]


@dataclass
class PushEntry:
    """(node, material) 就绪跟踪条目（缺块补发查表 + stats）。"""

    material_id: str
    node_id: str
    total: int
    chunk_size: int
    sha256: str
    #: 元包载荷（整包补推复用；只含 recipe / selections，不是材料副本）
    meta: dict[str, Any] = field(default_factory=dict[str, Any])
    #: ``material_wait``（预推在途）/ ``pushed``（预推完成）
    state: str = MATERIAL_WAIT
    #: 已发出的块 index（发出即计 pending；收 missing 按清单补发）
    pending: set[int] = field(default_factory=set[int])


class MaterialPusher:
    """材料分块推送器：预推 + missing 补发 + 就绪跟踪（DESIGN §5）。"""

    def __init__(
        self,
        ctx: Context,
        *,
        chunk_size: Callable[[], int],
        concurrency: Callable[[], int],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ctx = ctx
        self._chunk_size = chunk_size
        self._concurrency = concurrency
        self._clock = clock
        #: (node_id, material_id) → 就绪跟踪条目
        self._entries: dict[tuple[str, str], PushEntry] = {}
        self._chunks_resent: int = 0
        self._push_failed: int = 0
        self._no_nodes_warned: bool = False

    # -- stats（§14） -------------------------------------------------------
    @property
    def push_failed(self) -> int:
        """预推失败节点数（累计）。"""
        return self._push_failed

    @property
    def chunks_resent(self) -> int:
        """缺块补发的块数（累计）。"""
        return self._chunks_resent

    def pushing_count(self) -> int:
        """预推在途数（``material_wait`` 状态条目）。"""
        return sum(1 for entry in self._entries.values() if entry.state == MATERIAL_WAIT)

    # -- 候选节点（§5.2 / §6.1 共用口径） ------------------------------------
    def candidate_nodes(self) -> list[str]:
        """``executor/pytest`` 能力候选节点（保序去重；``ctx.nodes`` 缺席回空）。

        ``ctx.nodes.select`` 的 ``kinds`` 过滤的是**能力信封 kind**
        （:func:`common.capabilities.select_capabilities` 与调度器先例均为
        ``kinds=[executor.kind]``），故传 ``["executor"]`` 后再按条目
        ``kind=="executor" and id=="pytest"`` 收敛；labels 不做超集匹配
        （选择器空 labels 即可）。
        """
        service = self._ctx.get("nodes")
        if service is None:
            if not self._no_nodes_warned:
                self._no_nodes_warned = True
                self._ctx.logger.warning("无 ctx.nodes 服务：预推 / inspect 无候选节点")
            return []
        names: list[str] = []
        for entry in service.select(kinds=[CAP_KIND], labels=None):
            if (
                entry.get("kind") == CAP_KIND
                and entry.get("id") == CAP_ID
                and entry.get("node_id")
            ):
                node_id = str(entry["node_id"])
                if node_id not in names:
                    names.append(node_id)
        return names

    # -- 预推（§5.2 主路径） -------------------------------------------------
    async def push_material(
        self,
        material_id: str,
        *,
        selection_ids: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        """把材料预推给全部候选节点；返回 ``{pushed: [node_id…], failed: […]}``。

        * 候选 = :meth:`candidate_nodes`；``push_concurrency`` 并发限流；
        * 每节点按序发元包 + chunk（AT_LEAST_ONCE）；selections 随元包同步；
        * 材料 zip 经 ``ctx.test_core.get_material_bytes`` 读取（不留副本）；
        * 材料 / 选择集不存在抛 :class:`MaterialNotFound`，选择集不属本材料
          抛 :class:`ValueError`。
        """
        core = require_core(self._ctx)
        material = await core.get_material(material_id)
        if material is None:
            raise MaterialNotFound(f"材料不存在：{material_id}")
        data = await core.get_material_bytes(material_id)
        if data is None:
            raise MaterialNotFound(f"材料本体不存在：{material_id}")
        selections = await self._load_selections(core, material_id, selection_ids)
        recipe = material.get("recipe")
        chunk_size = max(1, int(self._chunk_size()))
        chunks = split_chunks(data, chunk_size)
        meta_payload: dict[str, Any] = {
            "proto": PROTO,
            "material_id": material_id,
            "sha256": str(material.get("sha256") or ""),
            "recipe_fingerprint": str(material.get("recipe_fingerprint") or ""),
            "total": len(chunks),
            "chunk_size": chunk_size,
            "recipe": dict(cast("Mapping[str, Any]", recipe)) if isinstance(recipe, Mapping) else {},
        }
        if selections is not None:
            meta_payload["selections"] = selections

        nodes = self.candidate_nodes()
        errors: dict[str, str] = {}
        semaphore = asyncio.Semaphore(max(1, int(self._concurrency())))

        async def push_one(node_id: str) -> None:
            async with semaphore:
                try:
                    await self._push_to_node(node_id, material_id, meta_payload, chunks)
                except Exception as error:  # noqa: BLE001 —— 单节点失败不影响其余节点
                    self._push_failed += 1
                    errors[node_id] = str(error)
                    self._ctx.logger.warning(
                        "材料 %s 预推节点 %s 失败：%s", material_id, node_id, error
                    )
                else:
                    self._ctx.logger.info(
                        "材料 %s 预推节点 %s 完成（%s 块）", material_id, node_id, len(chunks)
                    )

        await asyncio.gather(*(push_one(node_id) for node_id in nodes))
        return {
            "pushed": [node_id for node_id in nodes if node_id not in errors],
            "failed": [
                {"node_id": node_id, "error": errors[node_id]}
                for node_id in nodes
                if node_id in errors
            ],
        }

    async def _push_to_node(
        self,
        node_id: str,
        material_id: str,
        meta_payload: Mapping[str, Any],
        chunks: Sequence[bytes],
    ) -> None:
        """单节点按序发元包 + chunk（AT_LEAST_ONCE）；发出即计 pending。"""
        entry = PushEntry(
            material_id=material_id,
            node_id=node_id,
            total=len(chunks),
            chunk_size=int(meta_payload.get("chunk_size") or 0),
            sha256=str(meta_payload.get("sha256") or ""),
            meta=dict(meta_payload),
        )
        self._entries[(node_id, material_id)] = entry
        self._ctx.comm.send_to(
            node_id, TOPIC_MATERIAL, dict(meta_payload), policy=dict(_AT_LEAST_ONCE)
        )
        for index, chunk in enumerate(chunks):
            self._ctx.comm.send_to(
                node_id,
                TOPIC_MATERIAL_CHUNK,
                self._chunk_payload(entry, index, chunk),
                policy=dict(_AT_LEAST_ONCE),
            )
            entry.pending.add(index)
            await asyncio.sleep(0)  # 让出事件循环：多块推送不饿死其他协程（限流可观测）
        entry.state = PUSH_DONE

    def _chunk_payload(self, entry: PushEntry, index: int, chunk: bytes) -> dict[str, Any]:
        """自描述分块载荷（字段逐字对齐执行端 ``_on_material_chunk`` 解析）。"""
        return {
            "proto": PROTO,
            "material_id": entry.material_id,
            "sha256": entry.sha256,
            "total": entry.total,
            "index": index,
            "data_b64": base64.b64encode(chunk).decode("ascii"),
        }

    async def _load_selections(
        self,
        core: TestCoreInterface,
        material_id: str,
        selection_ids: Sequence[str] | None,
    ) -> list[dict[str, Any]] | None:
        """selections 元包清单（§20-Q1 (a)）：``[{selection_id, case_ids}]``。

        ``selection_ids=None`` 不带 selections 字段；显式给定（含空列表）逐个
        经 ``ctx.test_core.get_selection`` 读取，不存在抛 :class:`MaterialNotFound`。
        """
        if selection_ids is None:
            return None
        rows: list[dict[str, Any]] = []
        for selection_id in selection_ids:
            selection = await core.get_selection(selection_id)
            if selection is None:
                raise MaterialNotFound(f"选择集不存在：{selection_id}")
            if str(selection.get("material_id") or "") != material_id:
                raise ValueError(f"选择集 {selection_id} 不属于材料 {material_id}")
            case_ids = selection.get("case_ids")
            rows.append(
                {
                    "selection_id": str(selection.get("selection_id") or selection_id),
                    "case_ids": [
                        str(case)
                        for case in cast("list[object]", case_ids)
                        if isinstance(case, str)
                    ]
                    if isinstance(case_ids, list)
                    else [],
                }
            )
        return rows

    # -- 缺块补发（§5.2 兜底） -----------------------------------------------
    async def resend_missing(self, payload: object) -> int:
        """``pytest.material.missing`` 收件：只补缺块（幂等）；返回补发块数。

        * ``missing`` 空清单 = 从端 total 未知（执行端 ``MaterialStore.missing``
          口径）→ 整包补推（元包 + 全部块）；
        * 非空清单只发列出的缺块（§5.2「主端只发缺块」）；
        * 无就绪跟踪记录（主端重启后）忽略并告警，请重新预推。
        """
        if not isinstance(payload, Mapping):
            return 0
        raw = cast("Mapping[str, Any]", payload)
        if _int_or(raw.get("proto"), 0) != PROTO:
            self._ctx.logger.warning(
                "pytest.material.missing proto 不匹配（%s），拒收", raw.get("proto")
            )
            return 0
        material_id = str(raw.get("material_id") or "")
        node_id = str(raw.get("node_id") or "")
        missing_raw = raw.get("missing")
        if not material_id or not node_id or not isinstance(missing_raw, list):
            self._ctx.logger.warning("pytest.material.missing 载荷不完整，丢弃")
            return 0
        entry = self._entries.get((node_id, material_id))
        if entry is None:
            self._ctx.logger.warning(
                "材料 %s 缺块请求来自未跟踪节点 %s，忽略（请重新预推）", material_id, node_id
            )
            return 0
        listed = [
            item
            for item in cast("list[object]", missing_raw)
            if isinstance(item, int) and not isinstance(item, bool)
        ]
        if listed:
            indices = sorted({index for index in listed if 0 <= index < entry.total})
            resend_meta = False
        else:
            indices = list(range(entry.total))
            resend_meta = True
        if not indices:
            return 0
        core = require_core(self._ctx)
        data = await core.get_material_bytes(material_id)
        if data is None:
            self._ctx.logger.error("材料 %s 本体不存在，缺块补发放弃", material_id)
            return 0
        chunks = split_chunks(data, entry.chunk_size)
        if len(chunks) != entry.total:
            self._ctx.logger.warning(
                "材料 %s 块数漂移（跟踪 %s / 现 %s），缺块补发放弃",
                material_id,
                entry.total,
                len(chunks),
            )
            return 0
        entry.state = MATERIAL_WAIT
        if resend_meta:
            self._ctx.comm.send_to(
                node_id, TOPIC_MATERIAL, dict(entry.meta), policy=dict(_AT_LEAST_ONCE)
            )
        for index in indices:
            self._ctx.comm.send_to(
                node_id,
                TOPIC_MATERIAL_CHUNK,
                self._chunk_payload(entry, index, chunks[index]),
                policy=dict(_AT_LEAST_ONCE),
            )
            entry.pending.add(index)
        self._chunks_resent += len(indices)
        entry.state = PUSH_DONE
        self._ctx.logger.info(
            "材料 %s 缺块补发：%s 共 %s 块（%s）",
            material_id,
            node_id,
            len(indices),
            "整包" if resend_meta else "缺块",
        )
        return len(indices)


def _int_or(raw: object, fallback: int) -> int:
    """宽松取 int（bool 拒绝）：int / float / 数字串收敛，其余回退。"""
    if isinstance(raw, bool):
        return fallback
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return fallback
    return fallback


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
    "MaterialPusher",
    "NoCandidateNode",
    "PushEntry",
    "require_core",
    "split_chunks",
]
