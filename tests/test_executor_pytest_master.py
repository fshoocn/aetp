"""pytest 执行器管理端（master_plugins/executor_pytest）测试（DESIGN §5/§6/§8/§16）。

覆盖（分组 TestCase，对齐任务清单 7 组）：

1. 分块推送：元包 / 自描述分块字段全（proto/material_id/sha256/total/index/
   data_b64）、64KiB 切分与 b64 回拼、``push_concurrency`` 并发限流峰值断言、
   单节点失败隔离、``{pushed, failed}`` 返回形态；
2. missing 补发：非空清单只补缺块、空清单整包补推（含元包）、``chunks_resent``
   计数、重复 missing 幂等（每轮只补清单内缺块）、非候选节点 / 坏 proto 忽略、
   候选节点未跟踪自愈整包补推（§5.2 兜底）；
3. selections 同步：push 带 ``selection_ids`` 时元包 ``selections`` 字段形态
   ``[{selection_id, case_ids}]``（执行端 ``MaterialStore`` 物化口径，喂真
   MaterialStore 验证落盘）；不带 / 空清单 / 悬空 / 他材料选择集校验；
4. inspect 编排：定向单节点（executor/pytest 能力过滤 + 最少在途优先）、
   request_id 幂等首个应答胜出、同材料并发合并复用 request_id、超时扫尾
   （``inspect_timeout_s`` 拨钟）、迟到应答刷新 + ``late_results_total``、
   无候选 :class:`NoCandidateNode`；
5. 应答落库联动：analyze→``put_problems``（state 依 ok/severity）、
   collect→``put_cases``（真核心栈断言 material / material_case 行）；
6. API 端点：6 端点成功 / 400 / 404 / 409 / 503 映射 + ``/healthz`` 挂载；
7. GC：``list_unreferenced`` → ``delete``（MaterialInUse / MaterialNotFound
   竞态跳过）。

测试引导：IsolatedAsyncioTestCase + 临时 config.ini 起最小栈（appconfig + db +
comm 替身（记录 send_to / report、可注入收件）+ test_core + FakeNodes +
ExecPytestMasterPlugin）；API 集成组另起 webapi 真 HTTP 栈（手法同
tests/test_test_core_materials.py）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import tempfile
import unittest
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from unittest import mock

from cordis_port import Context

from _helpers import arequest as _arequest
from _helpers import free_port as _free_port
from common.cordis_utils import unload_all, wait_until
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.db import DbPlugin
from common.plugins.webapi import WebApiPlugin
from common.test_contracts import MaterialInUse, MaterialNotFound
from master_plugins.executor_pytest.material import (
    PROTO,
    TOPIC_INSPECT,
    TOPIC_INSPECT_RESULT,
    TOPIC_MATERIAL,
    TOPIC_MATERIAL_CHUNK,
    TOPIC_MATERIAL_MISSING,
    NoCandidateNode,
)
from master_plugins.executor_pytest.plugin import ExecPytestMasterPlugin
from master_plugins.test_core import TestCorePlugin as CorePlugin
from slave_plugins.executor_pytest.plugin import MaterialStore

#: 默认分块大小（DESIGN §5.1：64KiB 原文）
DEFAULT_CHUNK = 65536


# ================================================================ 纯函数辅助
def _zip_bytes(files: Mapping[str, str | bytes]) -> bytes:
    """把 ``{zip 内相对路径: 内容}`` 打成 zip 字节（材料上传用）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in files.items():
            package.writestr(name, content)
    return buffer.getvalue()


def _cap(
    node_id: str,
    cap_id: str = "pytest",
    *,
    kind: str = "executor",
    labels: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """能力信封条目（FakeNodes 数据源；条目带 node_id，同 NodesInterface）。"""
    return {
        "node_id": node_id,
        "kind": kind,
        "id": cap_id,
        "provider": f"{kind}-{cap_id}",
        "version": "1.0.0",
        "labels": dict(labels or {}),
    }


def _inspect_result(
    request_id: str,
    material_id: str,
    *,
    mode: str = "analyze",
    ok: bool = True,
    problems: Sequence[Mapping[str, Any]] = (),
    cases: Sequence[Mapping[str, Any]] = (),
    node_id: str = "node-a",
    duration_s: float = 0.25,
) -> dict[str, Any]:
    """``pytest.inspect.result`` 应答载荷（族内消息形状，proto=1）。"""
    return {
        "proto": PROTO,
        "request_id": request_id,
        "material_id": material_id,
        "mode": mode,
        "node_id": node_id,
        "ok": ok,
        "problems": [dict(item) for item in problems],
        "cases": [dict(item) for item in cases],
        "duration_s": duration_s,
    }


def _problem(code: str = "deps.missing", *, severity: str = "warning") -> dict[str, Any]:
    return {"code": code, "path": "requirements.txt", "message": "缺依赖", "severity": severity}


class _Clock:
    """可控时间源（GC 时间窗 / inspect 超时拨钟免真实等待）。"""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeNodes:
    """假节点目录（NodesInterface）：kinds / labels 子集匹配，条目带 node_id。"""

    def __init__(self, caps: Sequence[Mapping[str, object]] = ()) -> None:
        self.caps: list[dict[str, object]] = [dict(entry) for entry in caps]

    def list(self, *, online_only: bool = False) -> list[dict[str, str]]:
        return [
            {
                "node_id": str(entry.get("node_id") or ""),
                "node_kind": "slave",
                "address": "",
                "status": "online",
            }
            for entry in self.caps
        ]

    def select(
        self,
        *,
        kinds: Sequence[str] | None = None,
        labels: Mapping[str, str] | None = None,
        online_only: bool = True,
    ) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for entry in self.caps:
            if kinds is not None and entry.get("kind") not in set(kinds):
                continue
            entry_labels = cast(Mapping[str, str], entry.get("labels") or {})
            if labels is not None and not all(
                entry_labels.get(key) == value for key, value in dict(labels).items()
            ):
                continue
            out.append(dict(entry))
        return out


class _FakeComm:
    """comm 通道替身（test_executor_pytest._FakeComm 同手法）：记录定向 / 上行、
    可注入收件、可指定不可达节点（单节点失败隔离靶）。"""

    def __init__(self) -> None:
        self.reports: list[tuple[str, dict[str, Any]]] = []
        self.directed: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_nodes: set[str] = set()
        self._handlers: dict[str, list[Callable[..., object]]] = {}

    def on(self, topic: str, handler: Callable[..., object]) -> Callable[[], None]:
        self._handlers.setdefault(topic, []).append(handler)

        def dispose() -> None:
            handlers = self._handlers.get(topic, [])
            if handler in handlers:
                handlers.remove(handler)

        return dispose

    def report(self, topic: str, payload: object, **_kwargs: object) -> str:
        self.reports.append((str(topic), dict(cast(Mapping[str, Any], payload))))
        return f"msg-{len(self.reports)}"

    def send_to(self, node: str, topic: str, payload: object, **_kwargs: object) -> str:
        if node in self.fail_nodes:
            raise RuntimeError(f"节点 {node} 不可达")
        self.directed.append((str(node), str(topic), dict(cast(Mapping[str, Any], payload))))
        return f"msg-{len(self.directed)}"

    def deliver(self, topic: str, payload: object) -> None:
        """模拟收件（调用 comm.on 登记的处理器）。"""
        for handler in list(self._handlers.get(topic, [])):
            handler(payload, {"node": "node-a"})


class _RaceDeleteCore:
    """test_core 代理：``delete_material`` 对指定材料抛竞态异常（GC 跳过路径）。"""

    def __init__(self, real: Any, errors: Mapping[str, BaseException]) -> None:
        self._real = real
        self._errors = dict(errors)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def delete_material(self, material_id: str) -> bool:
        error = self._errors.get(material_id)
        if error is not None:
            raise error
        return await self._real.delete_material(material_id)


class _FaultyCore:
    """test_core 代理：指定方法抛固定异常（API 错误映射测试）。"""

    def __init__(self, real: Any, faults: Mapping[str, BaseException]) -> None:
        self._real = real
        self._faults = dict(faults)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real, name)
        error = self._faults.get(name)
        if error is None or not callable(attr):
            return attr

        async def faulted(*_args: Any, **_kwargs: Any) -> Any:
            raise error

        return faulted


# ================================================================ 栈基类
class MasterStackTestCase(unittest.IsolatedAsyncioTestCase):
    """管理端最小栈基类：``boot()`` 按用例装配（appconfig + db + comm 替身 +
    test_core + FakeNodes + ExecPytestMasterPlugin）。"""

    context: Context | None
    core: Any
    master: Any
    comm: _FakeComm
    nodes: FakeNodes

    async def asyncSetUp(self) -> None:
        # 核心派发循环改手动：本组不派发 Job，挂起 1s 周期防后台干扰
        self._interval_patch = mock.patch(
            "master_plugins.test_core.plugin._DISPATCH_INTERVAL", 3600.0
        )
        self._interval_patch.start()
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.comm = _FakeComm()
        self.context = None
        self.core = None
        self.master = None
        self.nodes = FakeNodes()

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
            self.context = None
        self._interval_patch.stop()
        self._temp.cleanup()

    async def boot(
        self,
        *,
        config: Mapping[str, object] | None = None,
        caps: Sequence[Mapping[str, object]] | None = None,
    ) -> None:
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\n"
            "kind = master\n"
            "name = master-node\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'aetp.db').as_posix()}\n"
            "\n[comm]\n"
            "batch_interval = 0.05\n",
            encoding="utf-8",
        )
        context = Context()
        self.context = context
        context.provide("node_kind", "master")
        context.provide("node_name", "master-node")
        await context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await context.plugin(DbPlugin)
        context.provide("comm", self.comm)
        self.nodes = FakeNodes(list(caps) if caps is not None else [_cap("node-a")])
        context.provide("nodes", self.nodes)
        await context.plugin(CorePlugin)
        await context.plugin(ExecPytestMasterPlugin, dict(config or {}))
        self.core = context.test_core
        self.master = context.executor_pytest_master

    # ------------------------------------------------------------ 驱动辅助
    async def upload(
        self,
        *,
        name: str = "包A",
        data: bytes | None = None,
        recipe: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = data if data is not None else _zip_bytes({"cases/test_a.py": "def test_a():\n    assert True\n"})
        result = await self.core.upload_material(
            name=name,
            data=payload,
            recipe=dict(recipe or {}),
        )
        return dict(cast(Mapping[str, Any], result))

    def directed(self, topic: str) -> list[tuple[str, dict[str, Any]]]:
        return [(node, payload) for node, name, payload in self.comm.directed if name == topic]

    async def wait_directed(
        self, topic: str, count: int, *, timeout: float = 10.0
    ) -> list[tuple[str, dict[str, Any]]]:
        ok = await wait_until(lambda: len(self.directed(topic)) >= count, timeout=timeout, interval=0.02)
        if not ok:
            self.fail(f"等待 {count} 条 {topic} 超时，已发：{[(n, t) for n, t, _ in self.comm.directed]}")
        return self.directed(topic)

    async def wait_inspect_state(
        self, request_id: str, state: str, *, timeout: float = 10.0
    ) -> Mapping[str, Any]:
        """轮询 inspect 记录直到目标状态（应答落库是 spawn 任务，需等收尾）。"""
        ok = await wait_until(
            lambda: (status := self._inspect_sync(request_id)) is not None and status["state"] == state,
            timeout=timeout,
            interval=0.02,
        )
        status = self._inspect_sync(request_id)
        if not ok or status is None:
            self.fail(f"inspect {request_id} 未达 {state}：{status}")
        return status

    def _inspect_sync(self, request_id: str) -> Mapping[str, Any] | None:
        record = self.master._inspects.get(request_id)
        return record.to_dict() if record is not None else None


# ================================================================ 1. 分块推送
class PushTests(MasterStackTestCase):
    """预推：自描述分块 / 64KiB 切分 / 并发限流 / 失败隔离（§5.1/§5.2）。"""

    async def test_meta_and_chunks_self_described_with_64k_split(self) -> None:
        await self.boot()
        data = os.urandom(150_000)  # 64KiB × 2 + 18928 → 3 块
        material = await self.upload(data=data, recipe={"entry": "cases"})
        result = await self.master.push_material(str(material["id"]))
        self.assertEqual(result, {"pushed": ["node-a"], "failed": []})
        # 元包：自描述字段全（chunk 不等它，但预推按序先发）
        metas = self.directed(TOPIC_MATERIAL)
        self.assertEqual(len(metas), 1)
        node, meta = metas[0]
        self.assertEqual(node, "node-a")
        self.assertEqual(meta["proto"], PROTO)
        self.assertEqual(meta["material_id"], material["id"])
        self.assertEqual(meta["sha256"], material["sha256"])
        self.assertEqual(meta["recipe_fingerprint"], material["recipe_fingerprint"])
        self.assertEqual(meta["total"], 3)
        self.assertEqual(meta["chunk_size"], DEFAULT_CHUNK)
        self.assertEqual(meta["recipe"], {"entry": "cases"})
        self.assertNotIn("selections", meta)  # 未指定 selection_ids 不带字段
        # 分块：每条自描述（total / sha256 冗余进每条），b64 回拼 == 原文
        chunks = self.directed(TOPIC_MATERIAL_CHUNK)
        self.assertEqual(len(chunks), 3)
        rebuilt = b""
        for index, (chunk_node, payload) in enumerate(chunks):
            self.assertEqual(chunk_node, "node-a")
            self.assertEqual(payload["proto"], PROTO)
            self.assertEqual(payload["material_id"], material["id"])
            self.assertEqual(payload["sha256"], material["sha256"])
            self.assertEqual(payload["total"], 3)
            self.assertEqual(payload["index"], index)  # 按 index 升序发出
            piece = base64.b64decode(str(payload["data_b64"]), validate=True)
            self.assertEqual(len(piece), DEFAULT_CHUNK if index < 2 else 150_000 - 2 * DEFAULT_CHUNK)
            rebuilt += piece
        self.assertEqual(rebuilt, data)
        topics = [name for _node, name, _payload in self.comm.directed]
        self.assertEqual(
            topics,
            [TOPIC_MATERIAL, TOPIC_MATERIAL_CHUNK, TOPIC_MATERIAL_CHUNK, TOPIC_MATERIAL_CHUNK],
        )
        self.assertEqual(self.master.stats()["pushing"], 0)  # 预推完成不再在途

    async def test_push_concurrency_peak_respects_limit(self) -> None:
        await self.boot(config={"push_concurrency": 2, "push_chunk_bytes": 512})
        self.nodes.caps = [_cap(f"node-{index}") for index in range(4)]
        material = await self.upload(data=os.urandom(2_000))  # 4 块 / 节点
        pusher = self.master._pusher
        original = pusher._push_to_node
        active = 0
        peak = 0

        async def tracked(
            node_id: str, material_id: str, meta_payload: Mapping[str, Any], chunks: Sequence[bytes]
        ) -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await original(node_id, material_id, meta_payload, chunks)
            finally:
                active -= 1

        with mock.patch.object(pusher, "_push_to_node", tracked):
            result = await self.master.push_material(str(material["id"]))
        self.assertEqual(sorted(result["pushed"]), ["node-0", "node-1", "node-2", "node-3"])
        self.assertEqual(result["failed"], [])
        self.assertLessEqual(peak, 2)  # push_concurrency=2 是硬上限
        self.assertEqual(peak, 2)  # 并发确实发生（4 节点 ≥ 2 并行）

    async def test_single_node_failure_isolated_with_pushed_failed_shape(self) -> None:
        await self.boot(config={"push_chunk_bytes": 512})
        self.nodes.caps = [_cap("node-ok-1"), _cap("node-bad"), _cap("node-ok-2")]
        self.comm.fail_nodes.add("node-bad")
        material = await self.upload(data=os.urandom(1_200))  # 3 块 / 节点
        result = await self.master.push_material(str(material["id"]))
        self.assertEqual(result["pushed"], ["node-ok-1", "node-ok-2"])
        failed = result["failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["node_id"], "node-bad")
        self.assertIn("不可达", str(failed[0]["error"]))
        self.assertEqual(self.master.stats()["push_failed"], 1)
        # 失败隔离：其余节点完整收到元包 + 全部块；失败节点一条未发
        for node in ("node-ok-1", "node-ok-2"):
            self.assertEqual(len([p for n, p in self.directed(TOPIC_MATERIAL) if n == node]), 1)
            self.assertEqual(len([p for n, p in self.directed(TOPIC_MATERIAL_CHUNK) if n == node]), 3)
        self.assertEqual([p for n, p in self.directed(TOPIC_MATERIAL_CHUNK) if n == "node-bad"], [])


# ================================================================ 2. missing 补发
class MissingResendTests(MasterStackTestCase):
    """缺块补发：只补缺块 / 空清单整包 / chunks_resent / 幂等（§5.2 兜底）。"""

    async def _pushed_material(self) -> str:
        await self.boot(config={"push_chunk_bytes": 512})
        material = await self.upload(data=os.urandom(2_000))  # 4 块
        result = await self.master.push_material(str(material["id"]))
        self.assertEqual(result["pushed"], ["node-a"])
        self.comm.directed.clear()
        return str(material["id"])

    async def test_non_empty_missing_resends_only_listed_chunks(self) -> None:
        material_id = await self._pushed_material()
        self.comm.deliver(
            TOPIC_MATERIAL_MISSING,
            {"proto": PROTO, "material_id": material_id, "node_id": "node-a", "missing": [1, 3]},
        )
        rows = await self.wait_directed(TOPIC_MATERIAL_CHUNK, 2)
        await asyncio.sleep(0.1)  # 留窗口给多余补发暴露
        self.assertEqual([payload["index"] for _node, payload in rows], [1, 3])
        self.assertEqual(self.directed(TOPIC_MATERIAL), [])  # 非空清单不补元包
        self.assertEqual(len(self.directed(TOPIC_MATERIAL_CHUNK)), 2)
        self.assertEqual(self.master.stats()["chunks_resent"], 2)

    async def test_empty_missing_repushes_whole_package(self) -> None:
        material_id = await self._pushed_material()
        self.comm.deliver(
            TOPIC_MATERIAL_MISSING,
            {"proto": PROTO, "material_id": material_id, "node_id": "node-a", "missing": []},
        )
        rows = await self.wait_directed(TOPIC_MATERIAL_CHUNK, 4)
        self.assertEqual([payload["index"] for _node, payload in rows], [0, 1, 2, 3])
        self.assertEqual(len(self.directed(TOPIC_MATERIAL)), 1)  # 整包补推带元包
        self.assertEqual(self.master.stats()["chunks_resent"], 4)

    async def test_repeated_missing_is_idempotent(self) -> None:
        material_id = await self._pushed_material()
        payload = {"proto": PROTO, "material_id": material_id, "node_id": "node-a", "missing": [2]}
        self.comm.deliver(TOPIC_MATERIAL_MISSING, dict(payload))
        await self.wait_directed(TOPIC_MATERIAL_CHUNK, 1)
        self.comm.deliver(TOPIC_MATERIAL_MISSING, dict(payload))
        rows = await self.wait_directed(TOPIC_MATERIAL_CHUNK, 2)
        # 幂等：每轮只补清单内缺块，不随重复请求累积额外块 / 不退化整包
        self.assertEqual([item["index"] for _node, item in rows], [2, 2])
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.directed(TOPIC_MATERIAL_CHUNK)), 2)
        self.assertEqual(self.directed(TOPIC_MATERIAL), [])
        self.assertEqual(self.master.stats()["chunks_resent"], 2)

    async def test_non_candidate_node_bad_proto_and_bad_shape_ignored(self) -> None:
        material_id = await self._pushed_material()
        base = len(self.comm.directed)
        self.comm.deliver(
            TOPIC_MATERIAL_MISSING,
            {"proto": PROTO, "material_id": material_id, "node_id": "ghost-node", "missing": [0]},
        )
        self.comm.deliver(
            TOPIC_MATERIAL_MISSING,
            {"proto": 99, "material_id": material_id, "node_id": "node-a", "missing": [0]},
        )
        self.comm.deliver(
            TOPIC_MATERIAL_MISSING,
            {"proto": PROTO, "material_id": material_id, "node_id": "node-a", "missing": "bad"},
        )
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.comm.directed), base)
        self.assertEqual(self.master.stats()["chunks_resent"], 0)

    async def test_untracked_candidate_node_heals_with_full_repush(self) -> None:
        material_id = await self._pushed_material()
        pusher = self.master._pusher
        pusher._entries.clear()  # 模拟主端重启后跟踪丢失
        base = len(self.comm.directed)
        self.comm.deliver(
            TOPIC_MATERIAL_MISSING,
            {"proto": PROTO, "material_id": material_id, "node_id": "node-a", "missing": [0]},
        )
        await asyncio.sleep(0.1)
        sent = self.comm.directed[base:]
        self.assertEqual(len(self.directed(TOPIC_MATERIAL)), 1)  # 自愈整包含元包
        entry = pusher._entries[("node-a", material_id)]
        self.assertEqual(len(self.directed(TOPIC_MATERIAL_CHUNK)), entry.total)
        self.assertEqual(self.master.stats()["chunks_resent"], entry.total)
        self.assertEqual(entry.state, "pushed")
        self.assertEqual((sent[0][0], sent[0][1]), ("node-a", TOPIC_MATERIAL))  # 元包先发


# ================================================================ 3. selections 同步
class SelectionSyncTests(MasterStackTestCase):
    """selections 随元包同步（§20-Q1 (a)）：字段形态 = 执行端物化口径。"""

    async def test_meta_carries_selections_and_materializes_on_executor(self) -> None:
        await self.boot()
        material = await self.upload()
        material_id = str(material["id"])
        selection_id = await self.core.save_selection(
            material_id, "勾选", ["cases/a.py::t1", "cases/a.py::t2"]
        )
        result = await self.master.push_material(material_id, selection_ids=[selection_id])
        self.assertEqual(result["pushed"], ["node-a"])
        _node, meta = self.directed(TOPIC_MATERIAL)[0]
        self.assertEqual(
            meta["selections"],
            [{"selection_id": selection_id, "case_ids": ["cases/a.py::t1", "cases/a.py::t2"]}],
        )
        # 执行端物化口径：喂真 MaterialStore，selections/{sid}.json 落盘可展开
        store = MaterialStore(self.root / "executor-cache")
        store.ready_dir(material_id).mkdir(parents=True)
        store.set_meta(
            material_id,
            sha256=str(meta["sha256"]),
            recipe_fingerprint=str(meta["recipe_fingerprint"]),
            total=int(meta["total"]),
            chunk_size=int(meta["chunk_size"]),
            recipe=cast(Mapping[str, Any], meta["recipe"]),
            selections=meta["selections"],
        )
        selection_path = store.ready_dir(material_id) / "selections" / f"{selection_id}.json"
        self.assertTrue(selection_path.is_file())
        written = json.loads(selection_path.read_text(encoding="utf-8"))
        self.assertEqual(written, {"selection_id": selection_id, "case_ids": ["cases/a.py::t1", "cases/a.py::t2"]})

    async def test_selections_field_presence_variants(self) -> None:
        await self.boot()
        material = await self.upload()
        material_id = str(material["id"])
        await self.master.push_material(material_id)  # 不带 selection_ids
        await self.master.push_material(material_id, selection_ids=[])  # 显式空清单
        metas = self.directed(TOPIC_MATERIAL)
        self.assertNotIn("selections", metas[0][1])
        self.assertEqual(metas[1][1]["selections"], [])

    async def test_selection_validation_rejects_dangling_and_foreign(self) -> None:
        await self.boot()
        material = await self.upload()
        material_id = str(material["id"])
        with self.assertRaises(MaterialNotFound):
            await self.master.push_material(material_id, selection_ids=["sel-none"])
        other = await self.upload(name="包B", data=_zip_bytes({"b.py": "x = 1\n"}))
        foreign = await self.core.save_selection(str(other["id"]), "他材料", ["b.py::x"])
        with self.assertRaises(ValueError):
            await self.master.push_material(material_id, selection_ids=[foreign])


# ================================================================ 4. inspect 编排
class InspectOrchestrationTests(MasterStackTestCase):
    """inspect 编排：定向单节点 / 幂等 / 合并 / 超时扫尾 / 迟到（§6.1）。"""

    async def test_directed_single_node_least_inflight_first_with_cap_filter(self) -> None:
        await self.boot()
        self.nodes.caps = [
            _cap("node-b"),
            _cap("node-a"),
            _cap("node-c"),
            _cap("node-a", "other"),  # 能力 id 不符
            _cap("node-x", "pytest", kind="service"),  # kind 不符
        ]
        m1 = await self.upload(data=b"m-1")
        m2 = await self.upload(data=b"m-2")
        m3 = await self.upload(data=b"m-3")
        r1 = await self.master.inspect(str(m1["id"]), "analyze")
        r2 = await self.master.inspect(str(m2["id"]), "analyze")
        r3 = await self.master.inspect(str(m3["id"]), "collect")
        sends = self.directed(TOPIC_INSPECT)
        # 最少在途优先（同负载按节点名稳定排序），候选 = executor/pytest 能力
        self.assertEqual([node for node, _payload in sends], ["node-a", "node-b", "node-c"])
        payloads = [payload for _node, payload in sends]
        self.assertEqual([payload["request_id"] for payload in payloads], [r1, r2, r3])
        self.assertEqual([payload["mode"] for payload in payloads], ["analyze", "analyze", "collect"])
        for payload in payloads:
            self.assertEqual(payload["proto"], PROTO)
            self.assertTrue(payload["material_id"])

    async def test_request_id_first_response_wins(self) -> None:
        await self.boot()
        material = await self.upload()
        material_id = str(material["id"])
        request_id = await self.master.inspect(material_id, "analyze")
        first = _inspect_result(
            request_id, material_id, problems=[_problem("deps.missing", severity="warning")]
        )
        second = _inspect_result(
            request_id, material_id, ok=False, problems=[_problem("syntax.error", severity="error")]
        )
        self.comm.deliver(TOPIC_INSPECT_RESULT, dict(first))
        status = await self.wait_inspect_state(request_id, "done")
        self.comm.deliver(TOPIC_INSPECT_RESULT, dict(second))  # 重复应答：只认首个
        await asyncio.sleep(0.1)
        self.assertTrue(status["ok"])
        self.assertEqual([str(item["code"]) for item in status["problems"]], ["deps.missing"])
        self.assertFalse(status["late"])
        self.assertEqual(self.master.stats()["late_results_total"], 0)
        row = await self.core.get_material(material_id)
        assert row is not None
        self.assertEqual([str(item["code"]) for item in cast(list[Mapping[str, Any]], row["problems"])], ["deps.missing"])
        self.assertEqual(row["state"], "analyzed")

    async def test_same_material_concurrent_inspect_merges_request_id(self) -> None:
        await self.boot()
        material = await self.upload()
        material_id = str(material["id"])
        first = await self.master.inspect(material_id, "analyze")
        merged = await self.master.inspect(material_id, "collect")
        self.assertEqual(merged, first)  # 并发合并：复用在途 request_id
        self.assertEqual(len(self.directed(TOPIC_INSPECT)), 1)  # 只发一个请求
        self.comm.deliver(
            TOPIC_INSPECT_RESULT,
            _inspect_result(first, material_id, mode="analyze", problems=[_problem()]),
        )
        await self.wait_inspect_state(first, "done")
        fresh = await self.master.inspect(material_id, "collect")
        self.assertNotEqual(fresh, first)  # 终结后新请求 → 新 request_id
        self.assertEqual(len(self.directed(TOPIC_INSPECT)), 2)

    async def test_timeout_sweep_marks_failed_and_releases_slot(self) -> None:
        await self.boot()
        clock = _Clock()
        self.master._clock = clock
        material = await self.upload()
        material_id = str(material["id"])
        request_id = await self.master.inspect(material_id, "analyze")
        clock.advance(301.0)  # inspect_timeout_s 默认 300 秒（拨钟免真实等待）
        self.assertEqual(self.master.sweep_inspect_timeouts(), 1)
        status = await self.wait_inspect_state(request_id, "failed")
        self.assertEqual(status["error"], "inspect timeout")
        self.assertEqual(self.master.stats()["inspect_failed"], 1)
        self.assertEqual(self.master.sweep_inspect_timeouts(), 0)  # 已终结不重复计数
        # 槽位释放：同材料可发起新 inspect
        fresh = await self.master.inspect(material_id, "analyze")
        self.assertNotEqual(fresh, request_id)

    async def test_late_response_refreshes_record_and_counts(self) -> None:
        await self.boot()
        clock = _Clock()
        self.master._clock = clock
        material = await self.upload()
        material_id = str(material["id"])
        request_id = await self.master.inspect(material_id, "collect")
        clock.advance(301.0)
        self.assertEqual(self.master.sweep_inspect_timeouts(), 1)
        late = _inspect_result(
            request_id,
            material_id,
            mode="collect",
            cases=[{"case_id": "cases/a.py::t", "name": "t", "path": "cases/a.py", "markers": [], "params": {}}],
        )
        self.comm.deliver(TOPIC_INSPECT_RESULT, dict(late))
        status = await self.wait_inspect_state(request_id, "done")  # 迟到收下刷新
        self.assertTrue(status["late"])
        self.assertEqual(len(status["cases"]), 1)
        self.assertEqual(self.master.stats()["late_results_total"], 1)
        # 迟到应答仍走落库联动
        rows = await self.core.list_material_cases(material_id)
        self.assertEqual([str(row["case_id"]) for row in rows], ["cases/a.py::t"])
        # 幂等：再收一份不重复刷新 / 不重复计数
        self.comm.deliver(TOPIC_INSPECT_RESULT, dict(late))
        await asyncio.sleep(0.1)
        self.assertEqual(self.master.stats()["late_results_total"], 1)

    async def test_no_candidate_raises_no_candidate_node(self) -> None:
        await self.boot(caps=[])
        material = await self.upload()
        with self.assertRaises(NoCandidateNode):
            await self.master.inspect(str(material["id"]), "analyze")
        self.assertEqual(self.master.stats()["inspect_failed"], 1)
        with self.assertRaises(ValueError):
            await self.master.inspect(str(material["id"]), "bogus")
        with self.assertRaises(ValueError):
            await self.master.inspect("", "analyze")


# ================================================================ 5. 应答落库联动
class ResultLandingTests(MasterStackTestCase):
    """inspect 应答 → put_problems / put_cases（真核心栈断言行状态）。"""

    async def test_analyze_warning_lands_state_analyzed(self) -> None:
        await self.boot()
        material = await self.upload()
        material_id = str(material["id"])
        request_id = await self.master.inspect(material_id, "analyze")
        self.comm.deliver(
            TOPIC_INSPECT_RESULT,
            _inspect_result(request_id, material_id, problems=[_problem("deps.missing", severity="warning")]),
        )
        await self.wait_inspect_state(request_id, "done")
        row = await self.core.get_material(material_id)
        assert row is not None
        self.assertEqual(row["state"], "analyzed")  # ok 且无 error 级问题
        problems = cast(list[Mapping[str, Any]], row["problems"])
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["code"], "deps.missing")
        self.assertEqual(problems[0]["severity"], "warning")

    async def test_analyze_error_severity_marks_failed(self) -> None:
        await self.boot()
        material = await self.upload()
        material_id = str(material["id"])
        request_id = await self.master.inspect(material_id, "analyze")
        self.comm.deliver(
            TOPIC_INSPECT_RESULT,
            _inspect_result(
                request_id,
                material_id,
                problems=[_problem("syntax.error", severity="error")],  # ok=True 但有 error 级
            ),
        )
        await self.wait_inspect_state(request_id, "done")
        row = await self.core.get_material(material_id)
        assert row is not None
        self.assertEqual(row["state"], "failed")

    async def test_analyze_not_ok_marks_failed(self) -> None:
        await self.boot()
        material = await self.upload()
        material_id = str(material["id"])
        request_id = await self.master.inspect(material_id, "analyze")
        self.comm.deliver(
            TOPIC_INSPECT_RESULT,
            _inspect_result(request_id, material_id, ok=False, problems=[_problem("collect.error", severity="error")]),
        )
        status = await self.wait_inspect_state(request_id, "failed")
        self.assertFalse(status["ok"])
        row = await self.core.get_material(material_id)
        assert row is not None
        self.assertEqual(row["state"], "failed")
        self.assertEqual(self.master.stats()["inspect_failed"], 1)

    async def test_collect_lands_cases_into_material_case(self) -> None:
        await self.boot()
        material = await self.upload()
        material_id = str(material["id"])
        request_id = await self.master.inspect(material_id, "collect")
        self.comm.deliver(
            TOPIC_INSPECT_RESULT,
            _inspect_result(
                request_id,
                material_id,
                mode="collect",
                cases=[
                    {
                        "case_id": "cases/a.py::test_x[1]",
                        "name": "test_x[1]",
                        "path": "cases/a.py",
                        "markers": ["smoke"],
                        "params": {"id": "1"},
                    },
                    {"case_id": "cases/b.py::test_y", "name": "test_y", "path": "cases/b.py", "markers": [], "params": {}},
                ],
            ),
        )
        await self.wait_inspect_state(request_id, "done")
        rows = await self.core.list_material_cases(material_id)
        self.assertEqual([str(row["case_id"]) for row in rows], ["cases/a.py::test_x[1]", "cases/b.py::test_y"])
        self.assertEqual(rows[0]["markers"], ["smoke"])
        self.assertEqual(rows[0]["params"], {"id": "1"})
        self.assertEqual(rows[0]["path"], "cases/a.py")
        self.assertEqual(rows[0]["status"], "ok")

    async def test_landing_survives_missing_material(self) -> None:
        """落库时材料已被删（put_cases 抛 MaterialNotFound）：记录保留、不抛。"""
        await self.boot()
        material = await self.upload()
        material_id = str(material["id"])
        request_id = await self.master.inspect(material_id, "collect")
        await self.core.delete_material(material_id)
        self.comm.deliver(
            TOPIC_INSPECT_RESULT,
            _inspect_result(
                request_id,
                material_id,
                mode="collect",
                cases=[{"case_id": "cases/a.py::t", "name": "t", "path": "cases/a.py", "markers": [], "params": {}}],
            ),
        )
        status = await self.wait_inspect_state(request_id, "done")
        self.assertTrue(status["ok"])
        self.assertEqual(len(status["cases"]), 1)


# ================================================================ 6. API 端点
class ApiTests(unittest.IsolatedAsyncioTestCase):
    """族内管理 API（DESIGN §8）：6 端点成功 / 400 / 404 / 409 / 503 + healthz。"""

    context: Context | None
    core: Any
    nodes: FakeNodes
    comm: _FakeComm

    async def asyncSetUp(self) -> None:
        self._interval_patch = mock.patch(
            "master_plugins.test_core.plugin._DISPATCH_INTERVAL", 3600.0
        )
        self._interval_patch.start()
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.context = None
        self.core = None
        self.comm = _FakeComm()
        self.nodes = FakeNodes()
        self.base_url = ""

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
            self.context = None
        self._interval_patch.stop()
        self._temp.cleanup()

    async def boot(self, *, caps: Sequence[Mapping[str, object]] | None = None) -> None:
        port = _free_port()
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\nname = api-node\n\n[web]\n"
            f"port = {port}\n"
            "auth_token = test-token\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'aetp.db').as_posix()}\n"
            "\n[comm]\n"
            "batch_interval = 0.05\n",
            encoding="utf-8",
        )
        context = Context()
        self.context = context
        context.provide("node_kind", "master")
        context.provide("node_name", "api-node")
        await context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await context.plugin(DbPlugin)
        await context.plugin(WebApiPlugin)
        context.provide("comm", self.comm)
        self.nodes = FakeNodes(list(caps) if caps is not None else [_cap("node-a")])
        context.provide("nodes", self.nodes)
        await context.plugin(CorePlugin)
        await context.plugin(ExecPytestMasterPlugin)
        ready = await wait_until(
            lambda: context.get("executor_pytest_master_api") is not None, timeout=10.0, interval=0.05
        )
        if not ready:
            raise RuntimeError("ExecPytestMasterApiService 未在限时内挂载")
        self.core = context.test_core
        self.base_url = f"http://127.0.0.1:{port}"

    async def upload(self, *, name: str = "API 包", data: bytes | None = None) -> str:
        payload = data if data is not None else _zip_bytes({"cases/a.py": "def test_a():\n    pass\n"})
        result = await self.core.upload_material(name=name, data=payload, recipe={})
        return str(cast(Mapping[str, Any], result)["id"])

    async def api(
        self, path: str, *, method: str = "GET", body: bytes | None = None
    ) -> tuple[int, dict[str, Any]]:
        status, payload = await _arequest(
            self.base_url, path, method=method, body=body, content_type="application/json"
        )
        return status, cast("dict[str, Any]", json.loads(payload))

    # ------------------------------------------------------------ recipe
    async def test_recipe_put_success_404_400_409(self) -> None:
        await self.boot()
        material_id = await self.upload()
        status, body = await self.api(
            f"/api/pytest/materials/{material_id}/recipe",
            method="PUT",
            body=json.dumps({"entry": "cases"}).encode("utf-8"),
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(cast("dict[str, Any]", body["material"])["recipe"], {"entry": "cases"})
        self.assertEqual(cast("dict[str, Any]", body["material"])["state"], "uploaded")
        # 404：材料不存在
        status, body = await self.api(
            "/api/pytest/materials/m-nope/recipe", method="PUT", body=b"{}"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")
        # 400：非法 JSON / 非对象
        status, body = await self.api(
            f"/api/pytest/materials/{material_id}/recipe", method="PUT", body=b"{"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_json")
        status, body = await self.api(
            f"/api/pytest/materials/{material_id}/recipe", method="PUT", body=b"[1]"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "bad_request")
        # 409：MaterialInUse 竞态映射
        proxy = _FaultyCore(self.core, {"update_recipe": MaterialInUse("材料被引用")})
        with mock.patch(
            "master_plugins.executor_pytest.api_service.require_core", return_value=proxy
        ):
            status, body = await self.api(
                f"/api/pytest/materials/{material_id}/recipe", method="PUT", body=b"{}"
            )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "material_in_use")

    # ------------------------------------------------------------ selections
    async def test_selection_save_success_400_404(self) -> None:
        await self.boot()
        material_id = await self.upload()
        status, body = await self.api(
            f"/api/pytest/materials/{material_id}/selections",
            method="POST",
            body=json.dumps({"name": "勾选", "case_ids": ["cases/a.py::test_a"]}).encode("utf-8"),
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        selection_id = str(body["selection_id"])
        selection = await self.core.get_selection(selection_id)
        assert selection is not None
        self.assertEqual(selection["case_ids"], ["cases/a.py::test_a"])
        # 400：缺 name / case_ids 形态错 / 空体
        for payload in (
            json.dumps({"case_ids": ["x"]}).encode("utf-8"),
            json.dumps({"name": " ", "case_ids": ["x"]}).encode("utf-8"),
            json.dumps({"name": "s", "case_ids": ["x", 1]}).encode("utf-8"),
            json.dumps({"name": "s", "case_ids": "x"}).encode("utf-8"),
            b"",
        ):
            status, body = await self.api(
                f"/api/pytest/materials/{material_id}/selections", method="POST", body=payload
            )
            self.assertEqual(status, 400, payload)
            self.assertEqual(body["code"], "bad_request", payload)
        # 404：材料不存在
        status, body = await self.api(
            "/api/pytest/materials/m-nope/selections",
            method="POST",
            body=json.dumps({"name": "s", "case_ids": ["x"]}).encode("utf-8"),
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")

    # ------------------------------------------------------------ push
    async def test_push_success_404(self) -> None:
        await self.boot()
        material_id = await self.upload()
        status, body = await self.api(
            f"/api/pytest/materials/{material_id}/push", method="POST", body=b""
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["material_id"], material_id)
        self.assertEqual(body["pushed"], ["node-a"])
        self.assertEqual(body["failed"], [])
        # 404：材料不存在
        status, body = await self.api("/api/pytest/materials/m-nope/push", method="POST", body=b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")
        # 404：selection_ids 悬空
        status, body = await self.api(
            f"/api/pytest/materials/{material_id}/push",
            method="POST",
            body=json.dumps({"selection_ids": ["sel-none"]}).encode("utf-8"),
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")

    # ------------------------------------------------------------ inspect
    async def test_inspect_success_400_503_and_status_404(self) -> None:
        await self.boot()
        material_id = await self.upload()
        status, body = await self.api(
            f"/api/pytest/materials/{material_id}/inspect",
            method="POST",
            body=json.dumps({"mode": "analyze"}).encode("utf-8"),
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        request_id = str(body["request_id"])
        status, body = await self.api(f"/api/pytest/inspects/{request_id}")
        self.assertEqual(status, 200)
        self.assertEqual(
            cast("dict[str, Any]", body["inspect"])["material_id"], material_id
        )
        # 404：未知 request_id
        status, body = await self.api("/api/pytest/inspects/insp-none")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")
        # 400：mode 非法 / 缺失
        for payload in (json.dumps({"mode": "bogus"}).encode("utf-8"), b"{}"):
            status, body = await self.api(
                f"/api/pytest/materials/{material_id}/inspect", method="POST", body=payload
            )
            self.assertEqual(status, 400, payload)
            self.assertEqual(body["code"], "bad_request", payload)
        # 503：无候选节点（NoCandidateNode 映射）；换材料避开并发合并复用
        other_id = await self.upload(name="503 包", data=_zip_bytes({"b.py": "y = 2\n"}))
        self.nodes.caps.clear()
        status, body = await self.api(
            f"/api/pytest/materials/{other_id}/inspect",
            method="POST",
            body=json.dumps({"mode": "collect"}).encode("utf-8"),
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["code"], "no_candidates")

    # ------------------------------------------------------------ stats + health
    async def test_stats_endpoint_and_healthz_mounted(self) -> None:
        await self.boot()
        status, body = await self.api("/api/pytest/stats")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        stats = cast("dict[str, int]", body["stats"])
        for key in (
            "materials_total",
            "pushing",
            "push_failed",
            "inspecting",
            "inspect_failed",
            "chunks_resent",
            "late_results_total",
        ):
            self.assertIn(key, stats)
        # health 挂载：/healthz 汇总 executor_pytest_master 指标
        status, body = await self.api("/healthz")
        self.assertEqual(status, 200)
        metrics = cast("dict[str, Any]", body["metrics"])
        self.assertIn("executor_pytest_master", metrics)
        self.assertIn(
            "late_results_total", cast("dict[str, Any]", metrics["executor_pytest_master"])
        )


# ================================================================ 7. GC
class GcTests(MasterStackTestCase):
    """主端 GC：无引用超期删除；MaterialInUse / MaterialNotFound 竞态跳过（§3.2）。"""

    async def _old_material(self, name: str, data: bytes) -> str:
        result = await self.upload(name=name, data=data)
        return str(result["id"])

    async def test_gc_deletes_only_unreferenced_expired(self) -> None:
        await self.boot()
        clock = _Clock()
        self.master._clock = clock
        self.core._clock = clock
        old = await self._old_material("超期无引用", b"zip-a")
        referenced = await self._old_material("超期被引用", b"zip-b")
        await self.core.save_selection(referenced, "引用中", ["a::b"])  # 选择集即引用
        clock.advance(91 * 86400.0)  # material_retain_days 默认 90
        fresh = await self._old_material("未超期", b"zip-c")
        removed = await self.master._gc_materials()
        self.assertEqual(removed, [old])
        self.assertIsNone(await self.core.get_material(old))
        self.assertIsNotNone(await self.core.get_material(referenced))  # 有引用跳过
        self.assertIsNotNone(await self.core.get_material(fresh))  # 时间窗内不删

    async def test_gc_skips_material_in_use_and_not_found_races(self) -> None:
        await self.boot()
        clock = _Clock()
        self.master._clock = clock
        self.core._clock = clock
        in_use = await self._old_material("竞态被引用", b"zip-x")
        gone = await self._old_material("竞态已删", b"zip-y")
        freed = await self._old_material("正常清理", b"zip-z")
        clock.advance(91 * 86400.0)
        proxy = _RaceDeleteCore(
            self.core,
            {
                in_use: MaterialInUse("刚被引用"),
                gone: MaterialNotFound("刚被删"),
            },
        )
        with mock.patch(
            "master_plugins.executor_pytest.plugin.require_core", return_value=proxy
        ):
            removed = await self.master._gc_materials()
        self.assertEqual(removed, [freed])  # 竞态跳过不拉垮整轮
        self.assertIsNotNone(await self.core.get_material(in_use))
        self.assertIsNotNone(await self.core.get_material(gone))


if __name__ == "__main__":
    unittest.main()
