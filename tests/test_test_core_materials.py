"""test_core 材料服务面测试（executor_pytest DESIGN §3/§4/§18 + job.log 流 §10.4）。

覆盖（分组 TestCase，对齐任务清单 7 组）：

1. 上传：zip 落盘 / 字节回读、``(sha256, recipe_fingerprint)`` 去重两态
   （同内容同 recipe 字符串复用、同内容不同字符串新建；recipe = 执行器自定义
   不透明字符串，任意串合法、core 不解析）、API 层 material_max_bytes 超限 413；
2. 状态机：update_recipe 回 uploaded + ``test/material-state`` 事件、
   put_problems(analyzed/failed)、并发 / 迟到迁移的行一致性（§3.1）；
3. 用例：put_cases 全量替换幂等、list_material_cases、``material_id::case_id``
   复合主键去重（§3）；
4. 选择集：save_selection / get_selection、删除引用约束（选择集 / task.params /
   stage.params 三路拦截、无引用可删含文件与用例清单清理，§3.2）；
5. submit 悬空引用：material_id / selection_id 不存在 → InvalidSpec（§18 注）；
6. job.log 流：seq 幂等（logs_duplicate_total）、跳号（logs_lost_total）、
   test/job-log 事件载荷裁剪、log 插件在场入库映射（op/name/source）与缺席降级；
7. list_unreferenced GC 辅助口径（时间窗 + 引用排除，§3.2）。

测试引导：IsolatedAsyncioTestCase + 临时 config.ini 起最小栈（appconfig + db +
comm + test_core，手法同 tests/test_test_core.py）；材料 / job-log 事件经
``ctx.on`` 捕获；``test.job`` 行直接播种（免调度）；log 入库用服务桩断言映射；
API 集成组另起 webapi 全栈（手法同 tests/test_test_core_api.py）。
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import io
import json
import tempfile
import unittest
import zipfile
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from unittest import mock
from urllib.parse import quote, urlparse

from cordis_port import Context

from _helpers import arequest as _arequest
from _helpers import free_port as _free_port
from common.cordis_utils import unload_all, wait_until
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.comm import CommService
from common.plugins.db import DbPlugin
from common.plugins.webapi import WebApiPlugin
from common.test_contracts import (
    SCHEMA_VERSION,
    TEST_PROTO,
    ExecutorSelector,
    InvalidSpec,
    MaterialInUse,
    MaterialNotFound,
    ShardSpec,
    StageSpec,
    TaskSpec,
)
from master_plugins.test_core import TestCorePlugin as CorePlugin
from master_plugins.test_core.models import TestJob as JobRow

#: 材料 / job-log 事件名（common/event_contract.py 已登记）
EVENT_MATERIAL_STATE = "test/material-state"
EVENT_JOB_LOG = "test/job-log"

#: 最小可用材料内容（zip 内容对核心不透明，一条文本即可）
_MIN_ZIP: dict[str, str] = {"cases/test_a.py": "def test_a():\n    assert True\n"}


# ================================================================ 纯函数辅助
def _zip_bytes(files: Mapping[str, str | bytes]) -> bytes:
    """把 ``{zip 内相对路径: 内容}`` 打成 zip 字节（材料上传用）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in files.items():
            package.writestr(name, content)
    return buffer.getvalue()


def _stage(name: str = "unit", *, params: Mapping[str, Any] | None = None) -> StageSpec:
    return StageSpec(
        name=name,
        executor=ExecutorSelector(id="pytest"),
        params=dict(params or {}),
        shard=ShardSpec(mode="none"),
        timeout_s=0.0,
    )


def _spec(
    stages: Sequence[StageSpec] | None = None,
    *,
    name: str = "材料任务",
    params: Mapping[str, Any] | None = None,
) -> TaskSpec:
    return TaskSpec(
        name=name,
        stages=list(stages) if stages is not None else [_stage()],
        params=dict(params or {}),
    )


def _problem(code: str = "syntax.error", *, severity: str = "error", message: str = "坏") -> dict[str, Any]:
    return {"code": code, "path": "cases/bad.py", "message": message, "severity": severity}


def _log_payload(
    *,
    job_id: str = "j-log-1",
    attempt: int = 1,
    seq: int = 0,
    stream: str = "stdout",
    case_id: str = "",
    lines: Sequence[Mapping[str, Any]] = (),
    proto: int = TEST_PROTO,
) -> dict[str, Any]:
    """``test.job.log`` 上行载荷（LogChunk 的 dict 形态）。"""
    return {
        "proto": proto,
        "job_id": job_id,
        "attempt": attempt,
        "seq": seq,
        "stream": stream,
        "case_id": case_id,
        "lines": [dict(item) for item in lines],
    }


def _line(msg: str, *, level: int = 20, ts: float = 100.0) -> dict[str, Any]:
    return {"ts": ts, "level": level, "msg": msg}


class _Clock:
    """可控时间源（GC 时间窗测试免真实等待）。"""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _LogStub:
    """log 服务桩：记录 ``ingest_remote`` 的 (payload, meta)。"""

    def __init__(self) -> None:
        self.batches: list[tuple[dict[str, Any], Mapping[str, Any]]] = []

    def ingest_remote(self, payload: object, meta: object) -> None:
        self.batches.append(
            (
                dict(cast(Mapping[str, Any], payload)),
                cast(Mapping[str, Any], meta),
            )
        )


class _StubWebUi:
    """最小 webui 服务桩（TestCoreUiService 挂载用）。"""

    def __init__(self) -> None:
        self.registrations: list[dict[str, object]] = []

    def register_ui(self, contribution_id: str, **options: object) -> None:
        self.registrations.append({"id": contribution_id, **options})


# ================================================================ 栈基类
class MaterialsStackTestCase(unittest.IsolatedAsyncioTestCase):
    """材料服务面最小栈基类：``boot()`` 按用例装配（appconfig + db + comm + core）。"""

    context: Context | None

    async def asyncSetUp(self) -> None:
        # 派发循环改手动（挂起 1s 周期，材料用例不依赖调度推进）
        self._interval_patch = mock.patch(
            "master_plugins.test_core.plugin._DISPATCH_INTERVAL", 3600.0
        )
        self._interval_patch.start()
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.context: Context | None = None
        self.core: Any = None
        self.log_stub: _LogStub | None = None

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
            self.context = None
        self._interval_patch.stop()
        self._temp.cleanup()

    async def boot(self, *, config: Mapping[str, object] | None = None, with_log: bool = False) -> None:
        """装配 appconfig + db + comm + test_core（可选提供 log 服务桩）。"""
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
        await context.plugin(CommService)
        if with_log:
            self.log_stub = _LogStub()
            context.provide("log", self.log_stub)
        for name in (EVENT_MATERIAL_STATE, EVENT_JOB_LOG):
            context.on(name, self._make_collector(name))
        await context.plugin(CorePlugin, dict(config or {}))
        self.core = context.test_core

    def _make_collector(self, name: str) -> Callable[[Any], None]:
        def collect(payload: Any) -> None:
            if isinstance(payload, Mapping):
                self.events[name].append(dict(cast(Mapping[str, Any], payload)))

        return collect

    # ------------------------------------------------------------ 驱动辅助
    async def upload(
        self,
        *,
        name: str = "包A",
        data: bytes | None = None,
        recipe: str = "",
        labels: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = data if data is not None else _zip_bytes(_MIN_ZIP)
        result = await self.core.upload_material(
            name=name,
            data=payload,
            recipe=recipe,
            labels=dict(labels or {}),
        )
        return dict(cast(Mapping[str, Any], result))

    async def seed_job(self, *, job_id: str = "j-log-1", task_id: str = "t-log-1", attempt: int = 1) -> None:
        """直接播种一行 TestJob（job.log 测试要 task_id 映射，免调度派发）。"""
        await self.core._repo(JobRow).add(
            JobRow(
                id=job_id,
                stage_id="s-1",
                task_id=task_id,
                node_id="node-a",
                attempt=attempt,
                state="running",
                shard="",
                lease_ts=0.0,
                started_ts=0.0,
                finished_ts=0.0,
                error="",
                result_seq=0,
            )
        )

    async def send_log(self, payload: Mapping[str, Any], meta: Mapping[str, Any] | None = None) -> None:
        """经收件主路径喂一个日志包（直调 _on_job_log）。"""
        await self.core._on_job_log(dict(payload), dict(meta) if meta else None)

    def state_events(self) -> list[dict[str, Any]]:
        return list(self.events[EVENT_MATERIAL_STATE])

    def job_log_events(self) -> list[dict[str, Any]]:
        return list(self.events[EVENT_JOB_LOG])

    def ingest_entries(self) -> list[dict[str, Any]]:
        assert self.log_stub is not None, "boot(with_log=True) 后才可断言入库"
        entries: list[dict[str, Any]] = []
        for payload, _meta in self.log_stub.batches:
            entries.extend(dict(cast(Mapping[str, Any], row)) for row in payload.get("logs", []))
        return entries


# ================================================================ 1. 上传
class UploadTests(MaterialsStackTestCase):
    """上传：落盘 / 回读 / 指纹去重两态（§4.3）。"""

    async def test_upload_stores_bundle_and_reads_back_bytes(self) -> None:
        await self.boot()
        data = _zip_bytes(_MIN_ZIP)
        recipe = "--entry cases --args -q;{not json at all}"
        material = await self.upload(data=data, recipe=recipe)
        self.assertTrue(str(material["id"]).startswith("m-"))
        self.assertEqual(material["state"], "uploaded")
        self.assertEqual(material["size"], len(data))
        self.assertEqual(material["sha256"], hashlib.sha256(data).hexdigest())
        fingerprint = str(material["recipe_fingerprint"])
        self.assertEqual(len(fingerprint), 16)
        # 指纹 = sha256(串 utf-8 字节) 截 16 hex（不透明字符串只看字节）
        self.assertEqual(
            fingerprint, hashlib.sha256(recipe.encode("utf-8")).hexdigest()[:16]
        )
        self.assertEqual(material["recipe"], recipe)
        # 落盘 + 字节回读（内容不透明，逐字节一致）
        bundle = self.core._bundle_path(str(material["id"]))
        self.assertTrue(bundle.is_file())
        self.assertEqual(bundle.read_bytes(), data)
        self.assertEqual(await self.core.get_material_bytes(str(material["id"])), data)
        # 详情形态（uri 指向 zip 本体，local:/// 盘符形态；recipe 原样字符串）
        detail = await self.core.get_material(str(material["id"]))
        assert detail is not None
        self.assertEqual(detail["uri"], f"local:///{bundle.resolve().as_posix()}")
        self.assertEqual(detail["recipe"], recipe)
        self.assertEqual(detail["problems"], [])
        self.assertEqual(len(self.state_events()), 1)
        event = self.state_events()[0]
        self.assertEqual(event["material_id"], material["id"])
        self.assertEqual(event["from"], "")
        self.assertEqual(event["to"], "uploaded")
        self.assertEqual(event["schema_version"], SCHEMA_VERSION)

    async def test_dedup_same_content_same_recipe_reuses_material(self) -> None:
        await self.boot()
        data = _zip_bytes(_MIN_ZIP)
        first = await self.upload(name="第一", data=data, recipe="entry=cases")
        second = await self.upload(name="第二", data=data, recipe="entry=cases")
        self.assertEqual(second["id"], first["id"])
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(len(await self.core.list_materials()), 1)

    async def test_same_content_different_recipe_creates_new_material(self) -> None:
        await self.boot()
        data = _zip_bytes(_MIN_ZIP)
        first = await self.upload(data=data, recipe="entry=cases")
        second = await self.upload(data=data, recipe="entry=other")
        self.assertNotEqual(second["id"], first["id"])
        self.assertFalse(second["deduplicated"])
        self.assertEqual(len(await self.core.list_materials()), 2)
        # 指纹不同 → 配方变体互不污染（同内容不同字符串 = 不同测试工程）
        self.assertNotEqual(second["recipe_fingerprint"], first["recipe_fingerprint"])

    async def test_different_content_same_recipe_creates_new_material(self) -> None:
        await self.boot()
        first = await self.upload(data=_zip_bytes(_MIN_ZIP), recipe="entry=cases")
        second = await self.upload(data=_zip_bytes({"cases/test_b.py": "x = 1\n"}), recipe="entry=cases")
        self.assertNotEqual(second["id"], first["id"])

    async def test_default_recipe_is_empty_string(self) -> None:
        """recipe 缺省空串：任意字符串均合法的极端情形，指纹恒定（sha256(b"")）。"""
        await self.boot()
        material = await self.upload()
        self.assertEqual(material["recipe"], "")
        self.assertEqual(
            material["recipe_fingerprint"], hashlib.sha256(b"").hexdigest()[:16]
        )

    async def test_list_materials_filters(self) -> None:
        await self.boot()
        await self.upload(name="带标签", labels={"project": "alpha"})
        await self.upload(name="无标签", data=_zip_bytes({"b.py": "y = 2\n"}))
        self.assertEqual(len(await self.core.list_materials(state="uploaded")), 2)
        self.assertEqual(len(await self.core.list_materials(state="analyzed")), 0)
        rows = await self.core.list_materials(labels={"project": "alpha"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "带标签")
        self.assertEqual(len(await self.core.list_materials(limit=1, offset=1)), 1)


# ================================================================ 2. 状态机
class MaterialStateTests(MaterialsStackTestCase):
    """状态机：update_recipe / put_problems 与 test/material-state 事件（§3.1）。"""

    async def test_update_recipe_resets_state_and_emits(self) -> None:
        await self.boot()
        material = await self.upload(recipe="entry=cases")
        mid = str(material["id"])
        await self.core.put_problems(mid, [_problem()], state="analyzed")
        self.events[EVENT_MATERIAL_STATE].clear()
        updated = dict(cast(Mapping[str, Any], await self.core.update_recipe(mid, "entry=cases2")))
        self.assertEqual(updated["state"], "uploaded")
        self.assertEqual(updated["recipe"], "entry=cases2")
        # 指纹 = sha256(新串) 截 16 hex，与旧指纹不同（字符串语义）
        self.assertEqual(
            updated["recipe_fingerprint"],
            hashlib.sha256(b"entry=cases2").hexdigest()[:16],
        )
        self.assertNotEqual(updated["recipe_fingerprint"], material["recipe_fingerprint"])
        events = self.state_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from"], "analyzed")
        self.assertEqual(events[0]["to"], "uploaded")
        self.assertEqual(events[0]["reason"], "recipe updated")
        self.assertEqual(events[0]["schema_version"], SCHEMA_VERSION)

    async def test_put_problems_transitions_and_full_replace(self) -> None:
        await self.boot()
        material = await self.upload()
        mid = str(material["id"])
        self.assertTrue(await self.core.put_problems(mid, [_problem("a.error")], state="analyzed"))
        self.assertTrue(await self.core.put_problems(mid, [_problem("b.error"), _problem("c.warn", severity="warning")], state="failed"))
        detail = await self.core.get_material(mid)
        assert detail is not None
        self.assertEqual(detail["state"], "failed")
        # problems 全量替换（存最近一次，§3.1）
        self.assertEqual([item["code"] for item in cast(list[Mapping[str, Any]], detail["problems"])], ["b.error", "c.warn"])
        events = self.state_events()
        self.assertEqual([(e["from"], e["to"]) for e in events], [("", "uploaded"), ("uploaded", "analyzed"), ("analyzed", "failed")])

    async def test_put_problems_rejects_bad_state_and_missing_material(self) -> None:
        await self.boot()
        material = await self.upload()
        mid = str(material["id"])
        with self.assertRaises(ValueError):
            await self.core.put_problems(mid, [], state="analyzing")
        with self.assertRaises(ValueError):
            await self.core.put_problems(mid, [], state="uploaded")
        self.assertFalse(await self.core.put_problems("m-nope", [], state="analyzed"))

    async def test_update_recipe_requires_existing_material(self) -> None:
        await self.boot()
        with self.assertRaises(MaterialNotFound):
            await self.core.update_recipe("m-nope", "entry=x")

    async def test_late_inspect_result_after_recipe_update_lands(self) -> None:
        """迟到应答口径（§3.1）：配方改后迟到的 inspect 结果照收、刷新状态。"""
        await self.boot()
        material = await self.upload(recipe="entry=cases")
        mid = str(material["id"])
        await self.core.put_problems(mid, [_problem("old.error")], state="failed")
        await self.core.update_recipe(mid, "entry=cases2")
        self.assertTrue(await self.core.put_problems(mid, [_problem("late.error")], state="failed"))
        detail = await self.core.get_material(mid)
        assert detail is not None
        self.assertEqual(detail["state"], "failed")
        self.assertEqual([item["code"] for item in cast(list[Mapping[str, Any]], detail["problems"])], ["late.error"])

    async def test_put_problems_same_state_no_duplicate_event(self) -> None:
        await self.boot()
        material = await self.upload()
        mid = str(material["id"])
        await self.core.put_problems(mid, [_problem("first.error")], state="analyzed")
        self.events[EVENT_MATERIAL_STATE].clear()
        await self.core.put_problems(mid, [_problem("second.error")], state="analyzed")
        self.assertEqual(self.state_events(), [])  # 状态未变不发迁移事件
        detail = await self.core.get_material(mid)
        assert detail is not None
        self.assertEqual([item["code"] for item in cast(list[Mapping[str, Any]], detail["problems"])], ["second.error"])

    async def test_concurrent_updates_leave_consistent_row(self) -> None:
        """并发迁移语义：各行 UPDATE 原子，终态不撕裂（state 与 problems 同属一次写入）。"""
        await self.boot()
        material = await self.upload(recipe="entry=cases")
        mid = str(material["id"])
        late_problem = _problem("race.error")
        await asyncio.gather(
            self.core.update_recipe(mid, "raced"),
            self.core.put_problems(mid, [late_problem], state="failed"),
        )
        detail = await self.core.get_material(mid)
        assert detail is not None
        self.assertIn(detail["state"], ("uploaded", "failed"))
        self.assertEqual(detail["recipe"], "raced")  # 配方列恒为 update_recipe 的完整写入
        if detail["state"] == "failed":
            self.assertEqual(detail["problems"], [late_problem])
        events = self.state_events()
        self.assertGreaterEqual(len(events), 1)
        self.assertLessEqual(len(events), 3)


# ================================================================ 3. 用例
class MaterialCaseTests(MaterialsStackTestCase):
    """用例清单：全量替换幂等 + 复合主键去重（§3 material_case）。"""

    async def test_put_cases_full_replace_and_idempotent(self) -> None:
        await self.boot()
        mid = str((await self.upload())["id"])
        first: list[dict[str, Any]] = [
            {"case_id": "cases/a.py::t1", "name": "t1", "path": "cases/a.py", "markers": ["smoke"], "params": {}},
            {"case_id": "cases/a.py::t2", "name": "t2", "path": "cases/a.py", "markers": [], "params": {"id": "p1"}},
        ]
        self.assertEqual(await self.core.put_cases(mid, first), 2)
        # 全量替换：第二次写入顶掉第一次
        second: list[dict[str, Any]] = [{"case_id": "cases/b.py::t9", "name": "t9", "path": "cases/b.py", "markers": [], "params": {}}]
        self.assertEqual(await self.core.put_cases(mid, second), 1)
        rows = await self.core.list_material_cases(mid)
        self.assertEqual([row["case_id"] for row in rows], ["cases/b.py::t9"])
        # 幂等：同一批再写一遍行集不变
        self.assertEqual(await self.core.put_cases(mid, second), 1)
        rows = await self.core.list_material_cases(mid)
        self.assertEqual([row["case_id"] for row in rows], ["cases/b.py::t9"])

    async def test_put_cases_dedupes_case_id_composite_key(self) -> None:
        await self.boot()
        mid = str((await self.upload())["id"])
        cases = [
            {"case_id": "cases/a.py::t1", "name": "首条", "path": "cases/a.py"},
            {"case_id": "cases/a.py::t1", "name": "重复", "path": "cases/a.py"},
            {"case_id": "", "name": "空 id 跳过", "path": "cases/a.py"},
            {"case_id": "cases/a.py::t2", "name": "t2", "path": "cases/a.py"},
        ]
        self.assertEqual(await self.core.put_cases(mid, cases), 2)
        rows = await self.core.list_material_cases(mid)
        self.assertEqual([row["case_id"] for row in rows], ["cases/a.py::t1", "cases/a.py::t2"])
        self.assertEqual(rows[0]["name"], "首条")  # 复合主键命中保留首条
        self.assertEqual(rows[0]["id"], f"{mid}::cases/a.py::t1")

    async def test_list_material_cases_shape(self) -> None:
        await self.boot()
        mid = str((await self.upload())["id"])
        await self.core.put_cases(
            mid,
            [
                {
                    "case_id": "cases/a.py::t1[smoke]",
                    "name": "t1[smoke]",
                    "path": "cases/a.py",
                    "markers": ["skip"],
                    "params": {"id": "smoke"},
                    "status": "error",
                }
            ],
        )
        rows = await self.core.list_material_cases(mid)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["markers"], ["skip"])
        self.assertEqual(row["params"], {"id": "smoke"})
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["material_id"], mid)

    async def test_put_cases_requires_existing_material(self) -> None:
        await self.boot()
        with self.assertRaises(MaterialNotFound):
            await self.core.put_cases("m-nope", [{"case_id": "a::b"}])
        self.assertEqual(await self.core.list_material_cases("m-nope"), [])


# ================================================================ 4. 选择集 / 删除
class SelectionAndDeleteTests(MaterialsStackTestCase):
    """选择集 CRUD 与删除引用约束（§3.2 / D14 / D19）。"""

    async def test_save_and_get_selection(self) -> None:
        await self.boot()
        mid = str((await self.upload())["id"])
        sid = await self.core.save_selection(mid, "勾选A", ["a::t1", "a::t2"], created_by="tester")
        self.assertTrue(sid.startswith("sel-"))
        selection = await self.core.get_selection(sid)
        assert selection is not None
        self.assertEqual(selection["selection_id"], sid)
        self.assertEqual(selection["material_id"], mid)
        self.assertEqual(selection["name"], "勾选A")
        self.assertEqual(selection["case_ids"], ["a::t1", "a::t2"])
        self.assertEqual(selection["created_by"], "tester")
        self.assertIsNone(await self.core.get_selection("sel-nope"))

    async def test_save_selection_requires_material(self) -> None:
        await self.boot()
        with self.assertRaises(MaterialNotFound):
            await self.core.save_selection("m-nope", "x", ["a::b"])

    async def test_delete_blocked_by_selection_reference(self) -> None:
        await self.boot()
        mid = str((await self.upload())["id"])
        await self.core.save_selection(mid, "勾选", ["a::t1"])
        with self.assertRaises(MaterialInUse):
            await self.core.delete_material(mid)
        self.assertIsNotNone(await self.core.get_material(mid))

    async def test_delete_blocked_by_task_params_reference(self) -> None:
        await self.boot()
        mid = str((await self.upload())["id"])
        await self.core.submit(_spec(params={"material_id": mid}))
        with self.assertRaises(MaterialInUse):
            await self.core.delete_material(mid)

    async def test_delete_blocked_by_stage_params_reference(self) -> None:
        await self.boot()
        mid = str((await self.upload())["id"])
        await self.core.submit(_spec([_stage("unit", params={"material_id": mid})]))
        with self.assertRaises(MaterialInUse):
            await self.core.delete_material(mid)

    async def test_delete_unreferenced_cleans_row_file_and_cases(self) -> None:
        await self.boot()
        mid = str((await self.upload())["id"])
        await self.core.put_cases(mid, [{"case_id": "a::t1", "name": "t1", "path": "a.py"}])
        bundle_dir = self.core._bundle_path(mid).parent
        self.assertTrue(bundle_dir.is_dir())
        self.assertTrue(await self.core.delete_material(mid))
        self.assertIsNone(await self.core.get_material(mid))
        self.assertIsNone(await self.core.get_material_bytes(mid))
        self.assertFalse(bundle_dir.exists())  # 文件一并清理
        self.assertEqual(await self.core.list_material_cases(mid), [])  # 用例清单随材料清理

    async def test_delete_missing_material(self) -> None:
        await self.boot()
        with self.assertRaises(MaterialNotFound):
            await self.core.delete_material("m-nope")


# ================================================================ 5. submit 悬空引用
class SubmitRefTests(MaterialsStackTestCase):
    """submit 的 material_id / selection_id 防悬空（§18 注）。"""

    async def test_submit_rejects_missing_material_id(self) -> None:
        await self.boot()
        for where, spec in (
            ("task params", _spec(params={"material_id": "m-nope"})),
            ("stage params", _spec([_stage("unit", params={"material_id": "m-nope"})])),
        ):
            with self.subTest(where=where):
                with self.assertRaises(InvalidSpec):
                    await self.core.submit(spec)

    async def test_submit_rejects_missing_selection_id(self) -> None:
        await self.boot()
        for where, spec in (
            ("task params", _spec(params={"selection_id": "sel-nope"})),
            ("stage params", _spec([_stage("unit", params={"selection_id": "sel-nope"})])),
        ):
            with self.subTest(where=where):
                with self.assertRaises(InvalidSpec):
                    await self.core.submit(spec)

    async def test_submit_rejects_non_string_reference(self) -> None:
        await self.boot()
        with self.assertRaises(InvalidSpec):
            await self.core.submit(_spec(params={"material_id": 42}))
        with self.assertRaises(InvalidSpec):
            await self.core.submit(_spec(params={"selection_id": ["x"]}))

    async def test_submit_accepts_existing_references(self) -> None:
        await self.boot()
        mid = str((await self.upload())["id"])
        sid = await self.core.save_selection(mid, "勾选", ["a::t1"])
        task_id = await self.core.submit(
            _spec(params={"material_id": mid}, stages=[_stage("unit", params={"selection_id": sid})])
        )
        self.assertTrue(task_id.startswith("t-"))
        detail = await self.core.get_task(task_id)
        assert detail is not None
        self.assertEqual(detail["params"]["material_id"], mid)


# ================================================================ 6. job.log 流
class JobLogStreamTests(MaterialsStackTestCase):
    """test.job.log 收件：seq 幂等 / 跳号计数、事件裁剪、入库映射与缺席降级。"""

    async def test_seq_idempotent_drops_duplicates_and_counts(self) -> None:
        await self.boot(with_log=True)
        await self.seed_job()
        await self.send_log(_log_payload(seq=0, lines=[_line("第一行")]))
        await self.send_log(_log_payload(seq=0, lines=[_line("重复包")]))  # 重复丢弃
        await self.send_log(_log_payload(seq=1, lines=[_line("第三行")]))
        self.assertEqual(len(self.job_log_events()), 2)  # 重复包不转发
        self.assertEqual(self.ingest_entries()[0]["msg"], "第一行")
        metrics = self.core.health_metrics()
        self.assertEqual(metrics["logs_duplicate_total"], 1)
        self.assertEqual(metrics["logs_lost_total"], 0)

    async def test_seq_gap_counts_lost(self) -> None:
        await self.boot(with_log=True)
        await self.seed_job()
        await self.send_log(_log_payload(seq=0, lines=[_line("零")]))
        await self.send_log(_log_payload(seq=3, lines=[_line("三")]))  # 缺 1、2
        self.assertEqual(self.core.health_metrics()["logs_lost_total"], 2)
        self.assertEqual(self.core.health_metrics()["logs_duplicate_total"], 0)
        # 后续 seq 继续以游标推进：旧 seq 一律按重复丢弃
        await self.send_log(_log_payload(seq=1, lines=[_line("迟到")]))
        self.assertEqual(self.core.health_metrics()["logs_duplicate_total"], 1)

    async def test_event_payload_trimmed_and_ingest_full(self) -> None:
        await self.boot(with_log=True)
        await self.seed_job()
        lines = [_line(f"行{i}") for i in range(10)]
        await self.send_log(_log_payload(seq=0, case_id="cases/a.py::t1", lines=lines))
        events = self.job_log_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["schema_version"], SCHEMA_VERSION)
        self.assertEqual(event["task_id"], "t-log-1")
        self.assertEqual(event["job_id"], "j-log-1")
        self.assertEqual(event["attempt"], 1)
        self.assertEqual(event["seq"], 0)
        self.assertEqual(event["stream"], "stdout")
        self.assertEqual(event["case_id"], "cases/a.py::t1")
        # 事件载荷裁剪（_LOG_EVENT_LIMIT = 3），入库不受影响
        event_lines = cast(list[Mapping[str, Any]], event["lines"])
        self.assertEqual(len(event_lines), 3)
        self.assertEqual([line["msg"] for line in event_lines], ["行0", "行1", "行2"])
        self.assertEqual(len(self.ingest_entries()), 10)

    async def test_ingest_maps_op_name_source(self) -> None:
        await self.boot(with_log=True)
        await self.seed_job(job_id="j-map", task_id="t-map", attempt=2)
        meta = {"node": "node-a", "topic": "test.job.log", "ts": 1.0, "received_ts": 2.0, "id": "msg-1"}
        await self.send_log(
            _log_payload(job_id="j-map", attempt=2, seq=0, case_id="cases/a.py::t1", lines=[_line("带用例")]),
            meta,
        )
        await self.send_log(
            _log_payload(job_id="j-map", attempt=2, seq=1, stream="stderr", lines=[_line("无用例", level=30)]),
            meta,
        )
        assert self.log_stub is not None
        self.assertEqual(len(self.log_stub.batches), 2)
        for _payload, got_meta in self.log_stub.batches:
            self.assertEqual(got_meta.get("node"), "node-a")  # comm 收件 meta 透传（按 node 溯源）
        entries = self.ingest_entries()
        self.assertEqual(entries[0]["name"], "test.j-map.2")
        self.assertEqual(entries[0]["op"], "t-map")  # 追踪元组 op = task_id
        self.assertEqual(entries[0]["source"], "cases/a.py::t1")  # source = case_id
        self.assertEqual(entries[1]["source"], "stderr")  # 无 case 归属回落 stream
        self.assertEqual(entries[1]["level"], 30)

    async def test_unknown_job_still_forwards_with_empty_task(self) -> None:
        await self.boot(with_log=True)
        await self.send_log(_log_payload(job_id="j-unknown", lines=[_line("x")]))
        events = self.job_log_events()
        self.assertEqual(len(events), 1)
        self.assertNotIn("task_id", events[0])
        entries = self.ingest_entries()
        self.assertEqual(entries[0]["op"], "")

    async def test_log_plugin_absent_degrades_to_forward_only(self) -> None:
        await self.boot()  # 不提供 log 服务
        await self.seed_job()
        await self.send_log(_log_payload(seq=0, lines=[_line("只转发")]))
        self.assertEqual(len(self.job_log_events()), 1)  # 事件照常转发
        self.assertIsNone(self.context.get("log") if self.context is not None else None)

    async def test_bad_payloads_ignored(self) -> None:
        await self.boot(with_log=True)
        await self.seed_job()
        await self.send_log(_log_payload(seq=0, proto=99, lines=[_line("坏 proto")]))
        await self.send_log(_log_payload(seq=0, attempt=0, lines=[_line("坏 attempt")]))
        await self.send_log(_log_payload(seq=-1, lines=[_line("坏 seq")]))
        await self.send_log({"stream": "bogus", "job_id": "j-log-1", "attempt": 1, "seq": 0, "lines": []})
        self.assertEqual(self.job_log_events(), [])
        self.assertEqual(self.ingest_entries(), [])
        self.assertEqual(self.core.health_metrics()["logs_lost_total"], 0)


# ================================================================ 7. GC 辅助
class GcListUnreferencedTests(MaterialsStackTestCase):
    """list_unreferenced：时间窗过滤 + 引用排除（§3.2 主端 GC 辅助口径）。"""

    async def test_list_unreferenced_time_window(self) -> None:
        await self.boot()
        clock = _Clock()
        self.core._clock = clock
        old = await self.upload(name="旧材料")
        clock.advance(100.0)
        fresh = await self.upload(name="新材料", data=_zip_bytes({"b.py": "y = 2\n"}))
        rows = await self.core.list_unreferenced(older_than_ts=clock.now + 100.0)
        self.assertEqual({row["id"] for row in rows}, {old["id"], fresh["id"]})
        rows = await self.core.list_unreferenced(older_than_ts=clock.now - 50.0)
        self.assertEqual([row["id"] for row in rows], [old["id"]])  # 只有超期的旧材料
        rows = await self.core.list_unreferenced(older_than_ts=0.0)
        self.assertEqual(rows, [])

    async def test_list_unreferenced_excludes_referenced(self) -> None:
        await self.boot()
        clock = _Clock()
        self.core._clock = clock
        by_task = await self.upload(name="任务引用")
        by_stage = await self.upload(name="阶段引用", data=_zip_bytes({"b.py": "y = 2\n"}))
        by_selection = await self.upload(name="选择集引用", data=_zip_bytes({"c.py": "z = 3\n"}))
        free = await self.upload(name="无引用", data=_zip_bytes({"d.py": "w = 4\n"}))
        await self.core.submit(_spec(params={"material_id": str(by_task["id"])}))
        await self.core.submit(_spec([_stage("unit", params={"material_id": str(by_stage["id"])})]))
        await self.core.save_selection(str(by_selection["id"]), "勾选", ["a::b"])
        rows = await self.core.list_unreferenced(older_than_ts=clock.now + 100.0)
        self.assertEqual([row["id"] for row in rows], [free["id"]])


def _raw_upload(
    base_url: str, path: str, *, body: bytes, headers: Mapping[str, str]
) -> tuple[int, bytes]:
    """原样 HTTP POST（http.client）：可带 X-Material-Recipe 等自定义头。"""
    parsed = urlparse(base_url)
    connection = http.client.HTTPConnection(
        cast(str, parsed.hostname), parsed.port, timeout=10
    )
    merged = {"X-AETP-Token": "test-token", **headers}
    try:
        connection.request("POST", path, body=body, headers=merged)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


# ================================================================ API 集成
class MaterialApiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """材料 API（api_service）：上传 / 去重 / 字节下载 / 超限 413（HTTP 集成组）。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.context: Context | None = None
        self.base_url = ""

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
            self.context = None
        self._temp.cleanup()

    async def boot(self) -> None:
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
        await context.plugin(CommService)
        context.provide("webui", _StubWebUi())
        await context.plugin(CorePlugin, {"material_max_bytes": 4096})
        ready = await wait_until(
            lambda: context.get("test_core_api") is not None, timeout=10.0, interval=0.05
        )
        if not ready:
            raise RuntimeError("TestCoreApiService 未在限时内挂载")
        self.base_url = f"http://127.0.0.1:{port}"

    def _upload_path(self, *, name: str = "api 包", recipe: str | None = None) -> str:
        query = f"/api/test/materials?name={quote(name)}"
        if recipe is not None:
            query += f"&recipe={quote(recipe)}"
        return query

    async def test_upload_dedup_and_download_roundtrip(self) -> None:
        await self.boot()
        data = _zip_bytes(_MIN_ZIP)
        recipe = "--entry cases;{not json}"  # 不透明字符串：非 JSON 串也合法
        status, payload = await _arequest(
            self.base_url,
            self._upload_path(recipe=recipe),
            method="POST",
            body=data,
            content_type="application/zip",
        )
        self.assertEqual(status, 200)
        body = cast("dict[str, Any]", json.loads(payload))
        self.assertTrue(body["ok"])
        self.assertFalse(body["deduplicated"])
        self.assertEqual(body["material"]["recipe"], recipe)  # 原样字符串透传
        mid = str(body["material"]["id"])
        # 同内容同字符串 → 去重复用
        status, payload = await _arequest(
            self.base_url,
            self._upload_path(name="再传一次", recipe=recipe),
            method="POST",
            body=data,
            content_type="application/zip",
        )
        self.assertEqual(status, 200)
        self.assertTrue(cast("dict[str, Any]", json.loads(payload))["deduplicated"])
        # zip 内容字节回读
        status, payload = await _arequest(
            self.base_url, f"/api/test/materials/{mid}/download"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, data)

    async def test_upload_recipe_header_and_query_are_opaque_strings(self) -> None:
        """X-Material-Recipe 头 / query recipe 参数均为不透明字符串（core 不解析）。

        头携带非 JSON 串合法、原样回读；非 Latin-1 串同 X-Material-Name 的降级
        模式走 query ``recipe`` 参数兜底。
        """
        await self.boot()
        header_recipe = "--entry cases;{not json}"
        status, payload = await asyncio.to_thread(
            _raw_upload,
            self.base_url,
            self._upload_path(name="头配方"),
            body=_zip_bytes({"cases/h.py": "x = 1\n"}),
            headers={
                "Content-Type": "application/zip",
                "X-Material-Recipe": header_recipe,
            },
        )
        self.assertEqual(status, 200)
        body = cast("dict[str, Any]", json.loads(payload))
        self.assertEqual(body["material"]["recipe"], header_recipe)
        self.assertEqual(
            body["material"]["recipe_fingerprint"],
            hashlib.sha256(header_recipe.encode("utf-8")).hexdigest()[:16],
        )
        query_recipe = "配方 中文 {也不是 json}"
        status, payload = await _arequest(
            self.base_url,
            self._upload_path(name="query 配方", recipe=query_recipe),
            method="POST",
            body=_zip_bytes({"cases/q.py": "x = 2\n"}),
            content_type="application/zip",
        )
        self.assertEqual(status, 200)
        body = cast("dict[str, Any]", json.loads(payload))
        self.assertEqual(body["material"]["recipe"], query_recipe)

    async def test_oversize_upload_rejected_413(self) -> None:
        await self.boot()
        status, payload = await _arequest(
            self.base_url,
            self._upload_path(),
            method="POST",
            body=b"a" * 8192,
            content_type="application/zip",
        )
        self.assertEqual(status, 413)
        body = cast("dict[str, Any]", json.loads(payload))
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "payload_too_large")

    async def test_missing_material_404(self) -> None:
        await self.boot()
        status, payload = await _arequest(
            self.base_url, "/api/test/materials/m-nope"
        )
        self.assertEqual(status, 404)
        self.assertEqual(cast("dict[str, Any]", json.loads(payload))["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
