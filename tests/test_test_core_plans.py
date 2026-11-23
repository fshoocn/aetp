"""test_core 计划服务面测试（测试计划与前端信息架构设计 §4 / §5.5 / §11 裁决）。

覆盖（分组 TestCase）：

1. CRUD / 复制：save 新建（``p-`` id）/ 更新（updated_ts 维护、created_by 不动）、
   get 缺失 None、delete 缺失 False、list keyword/labels/分页、clone 语义
   （API 层 get+save 组合、name 加「副本」后缀，§4.3）；
2. steps 校验矩阵（保存与下发共用 ``_check_plan_steps`` 链，逐条断言消息）：
   重名 / 缺 name / 缺 executor.id / material 悬空 / selection 悬空 /
   selection 归属不符 / 材料归属不符 / material_id 冲突 / steps 空；
3. dispatch 层校验：overrides 未知键 / stage_params 未命中步骤名 / AETP_* 环境键
   （overrides.env 与 stage_params[*].env）/ 合并后 material_id 冲突（评审 #5/#7/#9）；
4. dispatch 组装：defaults→顶层、labels 注入 plan_id+plan_name（评审 #21）、
   env 合并次序（stage_params 浅合并 → env 增量注入后者胜，评审 #8）、
   数组整体替换、executor.kind 补默认、顶层 material_id 并入 params；
5. 幂等：同 key 两次 dispatch 同 task_id 且 deduplicated=True、空 key 每次新建、
   幂等域 (source="plan-dispatch") 与 api 提交隔离（评审 #13）；
6. 材料删除引用闭环：plan.steps 扫描拦截（评审 #12）；
7. 材料 executor_id 落库与过滤谓词（== X OR == ''，通用可见，§4.5）；
8. 编辑器 v2 步骤 schema（编辑器 v2 设计 §2/§3/§4/§5）：material 保存必填与
   旧顶层 material_id 读取兼容、cases 注入 targets（selected）/ 不注入（all）、
   指纹漂移与用例消失逐条报错、同名冲突（targets / selection_id / 顶层
   material_id）、executor.nodes allowlist 交集与 random 派发策略（Scheduler
   单测）、trigger / notify 落库与枚举校验。

测试引导：IsolatedAsyncioTestCase + 临时 config.ini 起最小栈（appconfig + db +
comm + test_core，手法同 tests/test_test_core_materials.py）；派发循环挂起
（_DISPATCH_INTERVAL 拉大），Job 只入队不派发。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from collections.abc import Awaitable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from unittest import mock

from cordis_port import Context

from common.cordis_utils import unload_all
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.comm import CommService
from common.plugins.db import DbPlugin
from common.test_contracts import (
    LABEL_PLAN_ID,
    LABEL_PLAN_NAME,
    ExecutorSelector,
    InvalidSpec,
    MaterialInUse,
    PlanNotFound,
    StageSpec,
    TaskSpec,
)
from master_plugins.test_core import TestCorePlugin as CorePlugin
from master_plugins.test_core.config import TestCoreConfig as CoreConfig
from master_plugins.test_core.models import TestTask as TaskRow
from master_plugins.test_core.scheduler import TASK_TIMEOUT_KEY, InflightStats, Scheduler

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


def _step(
    name: str = "单测",
    *,
    executor_id: str = "pytest",
    kind: str | None = None,
    material_id: str | None = None,
    material: Mapping[str, Any] | None = None,
    cases: Mapping[str, Any] | None = None,
    nodes: Any = None,
    strategy: str | None = None,
    params: Mapping[str, Any] | None = None,
    shard: Mapping[str, Any] | None = None,
    requires: Mapping[str, Any] | None = None,
    timeout_s: float = 0.0,
) -> dict[str, Any]:
    """构造计划步骤（编辑器 v2 设计 §2.1：material / cases / executor.nodes+strategy）。

    ``material_id`` 为旧顶层格式（读取兼容）；``material`` 为 v2 绑定快照。
    """
    executor: dict[str, Any] = {"id": executor_id, "labels": {}}
    if kind is not None:
        executor["kind"] = kind
    if nodes is not None:
        executor["nodes"] = (
            list(cast("Sequence[Any]", nodes)) if isinstance(nodes, (list, tuple)) else nodes
        )
    if strategy is not None:
        executor["strategy"] = strategy
    step: dict[str, Any] = {
        "name": name,
        "executor": executor,
        "params": dict(params or {}),
        "shard": dict(shard) if shard is not None else {"mode": "none", "hint": 0},
        "requires": dict(requires or {}),
        "timeout_s": timeout_s,
    }
    if material is not None:
        step["material"] = dict(material)
    if material_id is not None:
        step["material_id"] = material_id
    if cases is not None:
        step["cases"] = dict(cases)
    return step


def _task_spec(*, name: str = "临时任务", key: str = "") -> TaskSpec:
    """普通提交规格（幂等域隔离测试用，走既有 submit）。"""
    return TaskSpec(
        name=name,
        stages=[
            StageSpec(name="unit", executor=ExecutorSelector(id="pytest"), params={})
        ],
        idempotency_key=key,
    )


class _Clock:
    """可控时间源（updated_ts 维护测试免真实等待）。"""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ================================================================ 栈基类
class PlansStackTestCase(unittest.IsolatedAsyncioTestCase):
    """计划服务面最小栈基类：``boot()`` 装配 appconfig + db + comm + test_core。"""

    context: Context | None

    async def asyncSetUp(self) -> None:
        # 派发循环改手动（挂起 1s 周期，计划用例只验落库/组装，Job 入队即可）
        self._interval_patch = mock.patch(
            "master_plugins.test_core.plugin._DISPATCH_INTERVAL", 3600.0
        )
        self._interval_patch.start()
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.context: Context | None = None
        self.core: Any = None
        self._default_material: dict[str, Any] | None = None

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
            self.context = None
        self._interval_patch.stop()
        self._temp.cleanup()

    async def boot(self) -> None:
        """装配 appconfig + db + comm + test_core。"""
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
        await context.plugin(CorePlugin)
        self.core = context.test_core

    # ------------------------------------------------------------ 驱动辅助
    async def default_material(self) -> dict[str, Any]:
        """默认通用材料（懒建缓存）：未显式绑材料的步骤统一补它（v2 保存必填）。"""
        if self._default_material is None:
            self._default_material = await self.upload(
                name="默认包",
                data=_zip_bytes({"cases/default.py": "def test_d():\n    assert True\n"}),
            )
        return self._default_material

    def binding(self, material: Mapping[str, Any]) -> dict[str, Any]:
        """v2 material 绑定快照（指纹取材料行现值，编辑器 v2 设计 §2.1）。"""
        return {
            "material_id": str(material["id"]),
            "sha256": str(material["sha256"]),
            "recipe_fingerprint": str(material["recipe_fingerprint"]),
            "name": str(material["name"]),
        }

    async def upload(
        self,
        *,
        name: str = "脚本包",
        data: bytes | None = None,
        recipe: str = "",
        executor_id: str = "",
    ) -> dict[str, Any]:
        payload = data if data is not None else _zip_bytes(_MIN_ZIP)
        result = await self.core.upload_material(
            name=name,
            data=payload,
            recipe=recipe,
            executor_id=executor_id,
        )
        return dict(cast(Mapping[str, Any], result))

    async def save(
        self,
        *,
        name: str = "计划A",
        plan_id: str = "",
        description: str = "",
        labels: Mapping[str, str] | None = None,
        defaults: Mapping[str, Any] | None = None,
        steps: Sequence[Mapping[str, Any]] | None = None,
        trigger: Mapping[str, Any] | None = None,
        notify: Mapping[str, Any] | None = None,
        created_by: str = "tester",
    ) -> dict[str, Any]:
        step_list = list(steps) if steps is not None else [_step()]
        # v2 保存必填 material：未绑材料的步骤补默认材料（既有用例统一口径）
        filled: list[Mapping[str, Any]] = []
        for step in step_list:
            if "material" not in step and not step.get("material_id"):
                material = await self.default_material()
                filled.append({**step, "material": self.binding(material)})
            else:
                filled.append(step)
        result = await self.core.save_plan(
            plan_id=plan_id,
            name=name,
            description=description,
            labels=dict(labels or {}),
            defaults=dict(defaults or {}),
            steps=filled,
            trigger=dict(trigger) if trigger is not None else None,
            notify=dict(notify) if notify is not None else None,
            created_by=created_by,
        )
        return dict(cast(Mapping[str, Any], result))

    async def invalid_message(self, awaitable: Awaitable[Any]) -> str:
        """断言 awaitable 抛 InvalidSpec 并返回消息（逐条断言用）。"""
        with self.assertRaises(InvalidSpec) as ctx:
            await awaitable
        return str(ctx.exception)


# ================================================================ 1. CRUD / 复制
class PlanCrudTests(PlansStackTestCase):
    """save / get / list / delete / clone 语义（§4.3 / §4.4）。"""

    async def test_save_creates_plan_and_get_roundtrip(self) -> None:
        await self.boot()
        plan = await self.save(
            name="回归计划",
            description="每晚跑",
            labels={"team": "qa"},
            defaults={"priority": 5},
        )
        self.assertTrue(str(plan["plan_id"]).startswith("p-"))
        self.assertEqual(plan["id"], plan["plan_id"])
        self.assertEqual(plan["name"], "回归计划")
        self.assertEqual(plan["description"], "每晚跑")
        self.assertEqual(plan["labels"], {"team": "qa"})
        self.assertEqual(plan["defaults"], {"priority": 5})
        self.assertEqual(plan["created_by"], "tester")
        steps = cast(list[Mapping[str, Any]], plan["steps"])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["name"], "单测")
        detail = await self.core.get_plan(str(plan["plan_id"]))
        assert detail is not None
        self.assertEqual(detail["name"], "回归计划")
        self.assertEqual(detail["steps"], plan["steps"])
        self.assertEqual(detail["labels"], {"team": "qa"})

    async def test_get_missing_plan_returns_none(self) -> None:
        await self.boot()
        self.assertIsNone(await self.core.get_plan("p-nope"))

    async def test_update_missing_plan_raises_plan_not_found(self) -> None:
        await self.boot()
        with self.assertRaises(PlanNotFound):
            await self.core.save_plan(plan_id="p-nope", name="x", steps=[_step()])

    async def test_update_replaces_fields_and_touches_updated_ts(self) -> None:
        await self.boot()
        clock = _Clock(1_000.0)
        self.core._clock = clock
        plan = await self.save(name="旧名")
        clock.advance(60.0)
        updated = await self.save(
            plan_id=str(plan["plan_id"]),
            name="新名",
            description="改了",
            labels={"k": "v"},
            steps=[_step("冒烟")],
        )
        self.assertEqual(updated["name"], "新名")
        self.assertEqual(updated["description"], "改了")
        self.assertEqual(updated["labels"], {"k": "v"})
        self.assertEqual(updated["created_ts"], 1_000.0)
        self.assertEqual(updated["updated_ts"], 1_060.0)
        self.assertEqual(updated["created_by"], "tester")  # 更新不动 created_by
        detail = await self.core.get_plan(str(plan["plan_id"]))
        assert detail is not None
        self.assertEqual(detail["name"], "新名")
        steps = cast(list[Mapping[str, Any]], detail["steps"])
        self.assertEqual(steps[0]["name"], "冒烟")

    async def test_delete_semantics_missing_false_removed_true(self) -> None:
        await self.boot()
        self.assertFalse(await self.core.delete_plan("p-nope"))
        plan = await self.save()
        plan_id = str(plan["plan_id"])
        self.assertTrue(await self.core.delete_plan(plan_id))
        self.assertIsNone(await self.core.get_plan(plan_id))
        self.assertFalse(await self.core.delete_plan(plan_id))

    async def test_list_plans_keyword_labels_and_paging(self) -> None:
        await self.boot()
        await self.save(
            name="回归计划", description="每晚跑", labels={"team": "qa", "run": "nightly"}
        )
        await self.save(name="冒烟计划", description="快速验证", labels={"team": "qa"})
        await self.save(name="发布检查", description="回归子集", labels={"team": "rel"})

        def names(rows: list[Mapping[str, Any]]) -> set[str]:
            return {str(row["name"]) for row in rows}

        self.assertEqual(
            names(await self.core.list_plans()),
            {"回归计划", "冒烟计划", "发布检查"},
        )
        # keyword 模糊：命中 name 或 description（大小写不敏感）
        self.assertEqual(names(await self.core.list_plans(keyword="回归")), {"回归计划", "发布检查"})
        self.assertEqual(names(await self.core.list_plans(keyword="验证")), {"冒烟计划"})
        self.assertEqual(names(await self.core.list_plans(keyword="SMOKE")), set())
        # labels 子集匹配（同 list_tasks 先例）
        self.assertEqual(names(await self.core.list_plans(labels={"team": "qa"})), {"回归计划", "冒烟计划"})
        self.assertEqual(
            names(await self.core.list_plans(labels={"team": "qa", "run": "nightly"})),
            {"回归计划"},
        )
        self.assertEqual(len(await self.core.list_plans(limit=1, offset=1)), 1)

    async def test_clone_composes_get_and_save_with_copy_suffix(self) -> None:
        """clone = API 层组合 get+save（§4.3）：name 加「副本」后缀、内容一致。"""
        await self.boot()
        plan = await self.save(
            name="回归计划", labels={"team": "qa"}, defaults={"priority": 5}
        )
        detail = await self.core.get_plan(str(plan["plan_id"]))
        assert detail is not None
        copy = await self.save(
            name=f"{detail['name']}副本",
            description=str(detail["description"]),
            labels=cast(Mapping[str, str], detail["labels"]),
            defaults=cast(Mapping[str, Any], detail["defaults"]),
            steps=cast(Sequence[Mapping[str, Any]], detail["steps"]),
        )
        self.assertNotEqual(copy["plan_id"], plan["plan_id"])
        self.assertEqual(copy["name"], "回归计划副本")
        self.assertEqual(copy["steps"], detail["steps"])
        self.assertEqual(copy["labels"], {"team": "qa"})
        self.assertEqual(copy["defaults"], {"priority": 5})
        original = await self.core.get_plan(str(plan["plan_id"]))
        assert original is not None
        self.assertEqual(original["name"], "回归计划")  # 原计划不动


# ================================================================ 2. steps 校验矩阵
class PlanStepValidationTests(PlansStackTestCase):
    """steps 校验矩阵（保存链，逐条断言消息；下发链同口径见 DispatchValidationTests）。"""

    async def test_empty_steps_rejected(self) -> None:
        await self.boot()
        message = await self.invalid_message(
            self.core.save_plan(name="空计划", steps=[])
        )
        self.assertIn("steps 不能为空", message)

    async def test_duplicate_step_names_rejected(self) -> None:
        await self.boot()
        message = await self.invalid_message(
            self.core.save_plan(name="重名计划", steps=[_step("单测"), _step("单测")])
        )
        self.assertIn("steps[1].name 重复：'单测'", message)

    async def test_missing_step_name_rejected(self) -> None:
        await self.boot()
        step = _step()
        step.pop("name")
        message = await self.invalid_message(
            self.core.save_plan(name="缺名计划", steps=[step])
        )
        self.assertIn("steps[0].name 需是非空字符串", message)

    async def test_missing_executor_id_rejected(self) -> None:
        await self.boot()
        step = _step()
        step["executor"] = {}
        message = await self.invalid_message(
            self.core.save_plan(name="缺执行器计划", steps=[step])
        )
        self.assertIn("steps[0].executor.id 需是非空字符串", message)

    async def test_problems_collected_one_by_one(self) -> None:
        """多个问题逐条列出（一次保存全部暴露）。"""
        await self.boot()
        step = _step()
        step.pop("name")
        step["executor"] = {}
        message = await self.invalid_message(
            self.core.save_plan(name="多问题计划", steps=[step])
        )
        self.assertIn("steps[0].name 需是非空字符串", message)
        self.assertIn("steps[0].executor.id 需是非空字符串", message)

    async def test_missing_plan_name_rejected(self) -> None:
        await self.boot()
        message = await self.invalid_message(
            self.core.save_plan(name="  ", steps=[_step()])
        )
        self.assertIn("name 需是非空字符串", message)

    async def test_dangling_material_id_rejected(self) -> None:
        await self.boot()
        message = await self.invalid_message(
            self.core.save_plan(
                name="悬空材料计划", steps=[_step(material_id="m-nope")]
            )
        )
        # 编辑器 v2 设计 §3：悬空 = 脚本版本已删除
        self.assertIn("steps[0] 脚本版本已删除，请重新选择脚本", message)

    async def test_dangling_selection_id_rejected(self) -> None:
        await self.boot()
        message = await self.invalid_message(
            self.core.save_plan(
                name="悬空选择集计划", steps=[_step(params={"selection_id": "sel-nope"})]
            )
        )
        self.assertIn("steps[0] 引用的 selection_id 不存在：sel-nope", message)

    async def test_selection_material_mismatch_rejected(self) -> None:
        await self.boot()
        mid_a = str((await self.upload(name="包A", data=_zip_bytes({"a.py": "x = 1\n"})))["id"])
        mid_b = str((await self.upload(name="包B", data=_zip_bytes({"b.py": "y = 2\n"})))["id"])
        sid = await self.core.save_selection(mid_b, "勾选B", ["a::t1"])
        message = await self.invalid_message(
            self.core.save_plan(
                name="归属不符计划",
                steps=[_step(material_id=mid_a, params={"selection_id": sid})],
            )
        )
        self.assertIn("steps[0] 的 selection 归属不符", message)
        self.assertIn(f"selection.material_id={mid_b!r}", message)
        self.assertIn(f"步骤 material_id={mid_a!r}", message)

    async def test_material_executor_mismatch_rejected(self) -> None:
        await self.boot()
        mid = str((await self.upload(name="canoe 包", executor_id="canoe"))["id"])
        message = await self.invalid_message(
            self.core.save_plan(
                name="归属不符计划",
                steps=[_step(executor_id="pytest", material_id=mid)],
            )
        )
        self.assertIn("steps[0] 引用的材料归属不符", message)
        self.assertIn("材料 executor_id='canoe'", message)
        self.assertIn("步骤 executor.id='pytest'", message)

    async def test_material_id_conflict_rejected(self) -> None:
        await self.boot()
        mid_a = str((await self.upload(name="包A", data=_zip_bytes({"a.py": "x = 1\n"})))["id"])
        mid_b = str((await self.upload(name="包B", data=_zip_bytes({"b.py": "y = 2\n"})))["id"])
        message = await self.invalid_message(
            self.core.save_plan(
                name="冲突计划",
                steps=[_step(material_id=mid_a, params={"material_id": mid_b})],
            )
        )
        self.assertIn("steps[0].material_id 与 params.material_id 同名冲突", message)
        self.assertIn(f"{mid_a!r} ≠ {mid_b!r}", message)

    async def test_accepts_matching_or_general_material(self) -> None:
        """材料归属匹配（material.executor_id ∈ {"", 步骤 executor.id}）放行。"""
        await self.boot()
        general = str((await self.upload(name="通用包", data=_zip_bytes({"a.py": "x = 1\n"})))["id"])
        pytest_pkg = str((await self.upload(name="pytest 包", data=_zip_bytes({"b.py": "y = 2\n"}), executor_id="pytest"))["id"])
        plan = await self.save(
            name="合法计划",
            steps=[
                _step("单测", executor_id="pytest", material_id=general),
                _step("单测二", executor_id="pytest", material_id=pytest_pkg),
            ],
        )
        self.assertTrue(str(plan["plan_id"]).startswith("p-"))

    async def test_save_validates_on_update_too(self) -> None:
        """保存链与下发共用：更新同样逐条校验。"""
        await self.boot()
        plan = await self.save(name="好计划")
        message = await self.invalid_message(
            self.core.save_plan(
                plan_id=str(plan["plan_id"]), name="改坏", steps=[]
            )
        )
        self.assertIn("steps 不能为空", message)


# ================================================================ 3. dispatch 层校验
class DispatchValidationTests(PlansStackTestCase):
    """dispatch overrides 校验（评审 #5/#7/#9）与共享校验链。"""

    async def test_dispatch_missing_plan_raises(self) -> None:
        await self.boot()
        with self.assertRaises(PlanNotFound):
            await self.core.dispatch_plan("p-nope", {"name": "x"})

    async def test_unknown_override_keys_collected_one_by_one(self) -> None:
        await self.boot()
        plan = await self.save()
        message = await self.invalid_message(
            self.core.dispatch_plan(
                str(plan["plan_id"]), {"zzz": 1, "extra": 2, "name": "ok"}
            )
        )
        self.assertIn("overrides 未知键：'zzz'", message)
        self.assertIn("overrides 未知键：'extra'", message)

    async def test_stage_params_key_must_hit_step_name(self) -> None:
        await self.boot()
        plan = await self.save(steps=[_step("单测")])
        message = await self.invalid_message(
            self.core.dispatch_plan(
                str(plan["plan_id"]), {"stage_params": {"不存在": {"x": 1}}}
            )
        )
        self.assertIn("stage_params 键未命中步骤名：'不存在'", message)

    async def test_reserved_env_keys_rejected_in_overrides_env(self) -> None:
        await self.boot()
        plan = await self.save()
        message = await self.invalid_message(
            self.core.dispatch_plan(
                str(plan["plan_id"]), {"env": {"AETP_TOKEN": "x", "TAG": "y"}}
            )
        )
        self.assertIn("overrides.env 含保留键：'AETP_TOKEN'", message)
        self.assertIn("AETP_* 禁止注入", message)
        self.assertNotIn("TAG", message)  # 普通键不误伤

    async def test_reserved_env_keys_rejected_in_stage_params_env(self) -> None:
        await self.boot()
        plan = await self.save(steps=[_step("单测")])
        message = await self.invalid_message(
            self.core.dispatch_plan(
                str(plan["plan_id"]),
                {"stage_params": {"单测": {"env": {"AETP_HOME": "z"}}}},
            )
        )
        self.assertIn("stage_params['单测'].env 含保留键：'AETP_HOME'", message)

    async def test_material_id_conflict_from_stage_params_rejected(self) -> None:
        await self.boot()
        mid_a = str((await self.upload(name="包A", data=_zip_bytes({"a.py": "x = 1\n"})))["id"])
        mid_b = str((await self.upload(name="包B", data=_zip_bytes({"b.py": "y = 2\n"})))["id"])
        plan = await self.save(steps=[_step("单测", material_id=mid_a)])
        message = await self.invalid_message(
            self.core.dispatch_plan(
                str(plan["plan_id"]),
                {"stage_params": {"单测": {"material_id": mid_b}}},
            )
        )
        self.assertIn("steps[0].material_id 与 params.material_id 同名冲突", message)
        self.assertIn(f"{mid_a!r} ≠ {mid_b!r}", message)

    async def test_dispatch_chain_catches_dangling_selection(self) -> None:
        """共享校验链在合并后步骤上跑：override 引入的悬空同样拦截。"""
        await self.boot()
        plan = await self.save(steps=[_step("单测")])
        message = await self.invalid_message(
            self.core.dispatch_plan(
                str(plan["plan_id"]),
                {"stage_params": {"单测": {"selection_id": "sel-nope"}}},
            )
        )
        self.assertIn("steps[0] 引用的 selection_id 不存在：sel-nope", message)


# ================================================================ 4. dispatch 组装
class DispatchAssemblyTests(PlansStackTestCase):
    """下发 = 实例化（§4.3/§5.5）：defaults / labels / 合并次序 / 转换规则。"""

    async def test_dispatch_assembles_task_with_merge_order(self) -> None:
        await self.boot()
        mid = str((await self.upload(name="pytest 包", executor_id="pytest"))["id"])
        mid2 = str(
            (await self.upload(name="canoe 包", data=_zip_bytes({"c.py": "z = 3\n"}), executor_id="canoe"))["id"]
        )
        plan = await self.save(
            name="回归计划",
            labels={"team": "qa"},
            defaults={"priority": 5, "failure_policy": "continue", "timeout_s": 3600},
            steps=[
                _step(
                    "单测",
                    executor_id="pytest",
                    material_id=mid,
                    params={"targets": ["tests/"], "env": {"A": "1", "TAG": "base"}},
                    shard={"mode": "plan", "hint": 4},
                    requires={"canoe": 1},
                    timeout_s=7200,
                ),
                _step("总线回归", executor_id="canoe", kind="executor", material_id=mid2, params={}),
            ],
        )
        plan_id = str(plan["plan_id"])
        result = await self.core.dispatch_plan(
            plan_id,
            {
                "name": "今晚回归",
                "priority": 9,
                "labels": {"run": "nightly"},
                "env": {"TAG": "ov", "C": "3"},
                "stage_params": {
                    "单测": {
                        "targets": ["a/", "b/"],
                        "extra": 1,
                        "env": {"TAG": "sp", "D": "4"},
                    }
                },
                "idempotency_key": "k1",
            },
        )
        task_id = str(result["task_id"])
        self.assertFalse(result["deduplicated"])
        detail = await self.core.get_task(task_id)
        assert detail is not None
        # 顶层：name/priority 覆盖，failure_policy/timeout 来自 defaults
        self.assertEqual(detail["name"], "今晚回归")
        self.assertEqual(detail["priority"], 9)
        self.assertEqual(detail["failure_policy"], "continue")
        self.assertEqual(detail["source"], "plan-dispatch")
        self.assertEqual(detail["idempotency_key"], "k1")
        # labels：plan.labels + overrides.labels + plan_id/plan_name 注入
        self.assertEqual(
            detail["labels"],
            {"team": "qa", "run": "nightly", LABEL_PLAN_ID: plan_id, LABEL_PLAN_NAME: "回归计划"},
        )
        # defaults.timeout_s 暂存任务 params（内部键，对外剥离）
        row = await self.core._repo(TaskRow).get(task_id)
        assert row is not None
        self.assertEqual(json.loads(row.params)[TASK_TIMEOUT_KEY], 3600.0)
        stages = cast(list[Mapping[str, Any]], detail["stages"])
        self.assertEqual([stage["name"] for stage in stages], ["单测", "总线回归"])
        first = stages[0]
        # executor.kind 缺省补 "executor"（§4.2 转换）；nodes/strategy 随 JSON 持久化（编辑器 v2 §4）
        self.assertEqual(
            first["executor"],
            {
                "id": "pytest",
                "kind": "executor",
                "labels": {},
                "nodes": [],
                "strategy": "balanced",
            },
        )
        self.assertEqual(first["shard_mode"], "plan")
        self.assertEqual(first["shard_hint"], 4)
        self.assertEqual(first["requires"], {"canoe": 1})
        self.assertEqual(first["timeout_s"], 7200)
        first_params = cast(dict[str, Any], first["params"])
        # 顶层 material_id 仅顶层有 → 并入 params.material_id
        self.assertEqual(first_params["material_id"], mid)
        # 浅合并：数组整体替换、同名键替换
        self.assertEqual(first_params["targets"], ["a/", "b/"])
        self.assertEqual(first_params["extra"], 1)
        # env 合并次序（评审 #8）：stage_params.env 整体替换 → env 增量注入后者胜
        self.assertEqual(first_params["env"], {"TAG": "ov", "D": "4", "C": "3"})
        self.assertNotIn("A", first_params["env"])  # stage_params.env 同名替换掉原 env
        # env 注入每步 params.env（含未被 stage_params 命中的步骤）；material_id 注入（§2.3）
        self.assertEqual(
            cast(dict[str, Any], stages[1]["params"]),
            {"env": {"TAG": "ov", "C": "3"}, "material_id": mid2},
        )

    async def test_dispatch_defaults_fill_top_level_without_overrides(self) -> None:
        await self.boot()
        plan = await self.save(
            name="回归计划",
            labels={"team": "qa"},
            defaults={"priority": 5, "failure_policy": "continue", "timeout_s": 3600},
        )
        result = await self.core.dispatch_plan(str(plan["plan_id"]))
        self.assertFalse(result["deduplicated"])
        detail = await self.core.get_task(str(result["task_id"]))
        assert detail is not None
        self.assertEqual(detail["name"], "回归计划")
        self.assertEqual(detail["priority"], 5)
        self.assertEqual(detail["failure_policy"], "continue")
        self.assertEqual(
            detail["labels"],
            {"team": "qa", LABEL_PLAN_ID: str(plan["plan_id"]), LABEL_PLAN_NAME: "回归计划"},
        )
        stages = cast(list[Mapping[str, Any]], detail["stages"])
        self.assertEqual(stages[0]["executor"]["kind"], "executor")


# ================================================================ 5. 幂等
class DispatchIdempotencyTests(PlansStackTestCase):
    """幂等判定（评审 #13）：(source="plan-dispatch", idempotency_key) 域。"""

    async def test_same_key_twice_returns_same_task_with_deduplicated(self) -> None:
        await self.boot()
        plan = await self.save()
        first = dict(
            cast(
                Mapping[str, Any],
                await self.core.dispatch_plan(
                    str(plan["plan_id"]), {"idempotency_key": "night-1"}
                ),
            )
        )
        second = dict(
            cast(
                Mapping[str, Any],
                await self.core.dispatch_plan(
                    str(plan["plan_id"]), {"idempotency_key": "night-1"}
                ),
            )
        )
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["task_id"], first["task_id"])

    async def test_empty_key_creates_new_task_each_time(self) -> None:
        await self.boot()
        plan = await self.save()
        first = dict(cast(Mapping[str, Any], await self.core.dispatch_plan(str(plan["plan_id"]))))
        second = dict(
            cast(
                Mapping[str, Any],
                await self.core.dispatch_plan(str(plan["plan_id"]), {"idempotency_key": ""}),
            )
        )
        self.assertFalse(first["deduplicated"])
        self.assertFalse(second["deduplicated"])
        self.assertNotEqual(second["task_id"], first["task_id"])

    async def test_plan_dispatch_key_isolated_from_api_submits(self) -> None:
        """幂等域隔离：api 提交的同名 key 不与 plan-dispatch 相撞。"""
        await self.boot()
        plan = await self.save()
        api_task = await self.core.submit(_task_spec(key="night-1"))
        result = dict(
            cast(
                Mapping[str, Any],
                await self.core.dispatch_plan(
                    str(plan["plan_id"]), {"idempotency_key": "night-1"}
                ),
            )
        )
        self.assertNotEqual(result["task_id"], api_task)
        self.assertFalse(result["deduplicated"])


# ================================================================ 6. 材料删除引用闭环
class MaterialDeleteClosureTests(PlansStackTestCase):
    """plan.steps 引用扫描（评审 #12）。"""

    async def test_delete_blocked_by_plan_top_level_material(self) -> None:
        await self.boot()
        mid = str((await self.upload())["id"])
        await self.save(steps=[_step(material_id=mid)])
        with self.assertRaises(MaterialInUse):
            await self.core.delete_material(mid)

    async def test_delete_blocked_by_plan_params_material(self) -> None:
        await self.boot()
        mid = str((await self.upload())["id"])
        # params.material_id 与 material 同值 = 无害重复（沿冲突先例），steps 扫描照拦删除
        await self.save(steps=[_step(material_id=mid, params={"material_id": mid})])
        with self.assertRaises(MaterialInUse):
            await self.core.delete_material(mid)

    async def test_delete_unreferenced_after_plan_deleted(self) -> None:
        await self.boot()
        mid = str((await self.upload())["id"])
        plan = await self.save(steps=[_step(material_id=mid)])
        self.assertTrue(await self.core.delete_plan(str(plan["plan_id"])))
        self.assertTrue(await self.core.delete_material(mid))
        self.assertIsNone(await self.core.get_material(mid))


# ================================================================ 7. 材料 executor_id
class MaterialExecutorFilterTests(PlansStackTestCase):
    """executor_id 落库与过滤谓词（== X OR == ''，通用可见，§4.5）。"""

    async def test_upload_records_executor_id(self) -> None:
        await self.boot()
        general = await self.upload(name="通用包", data=_zip_bytes({"a.py": "x = 1\n"}))
        pytest_pkg = await self.upload(
            name="pytest 包", data=_zip_bytes({"b.py": "y = 2\n"}), executor_id="pytest"
        )
        self.assertEqual(general["executor_id"], "")
        self.assertEqual(pytest_pkg["executor_id"], "pytest")
        detail = await self.core.get_material(str(pytest_pkg["id"]))
        assert detail is not None
        self.assertEqual(detail["executor_id"], "pytest")

    async def test_list_materials_executor_filter_keeps_general_visible(self) -> None:
        await self.boot()
        await self.upload(name="通用包", data=_zip_bytes({"a.py": "x = 1\n"}))
        await self.upload(
            name="pytest 包", data=_zip_bytes({"b.py": "y = 2\n"}), executor_id="pytest"
        )
        await self.upload(
            name="canoe 包", data=_zip_bytes({"c.py": "z = 3\n"}), executor_id="canoe"
        )

        def names(rows: list[Mapping[str, Any]]) -> set[str]:
            return {str(row["name"]) for row in rows}

        self.assertEqual(
            names(await self.core.list_materials(executor_id="pytest")),
            {"通用包", "pytest 包"},
        )
        self.assertEqual(
            names(await self.core.list_materials(executor_id="canoe")),
            {"通用包", "canoe 包"},
        )
        self.assertEqual(
            names(await self.core.list_materials()),
            {"通用包", "pytest 包", "canoe 包"},
        )


# ================================================================ 8. 编辑器 v2 步骤 schema
class PlanStepV2SchemaTests(PlansStackTestCase):
    """步骤 schema v2（编辑器 v2 设计 §2.1/§2.3）：material 必填 / cases 注入。"""

    async def test_material_required_on_save(self) -> None:
        await self.boot()
        message = await self.invalid_message(
            self.core.save_plan(name="无材料计划", steps=[_step()])
        )
        self.assertIn("steps[0].material 必填", message)

    async def test_material_shape_and_id_required(self) -> None:
        await self.boot()
        bad_shape = _step()
        bad_shape["material"] = "bad"
        message = await self.invalid_message(
            self.core.save_plan(name="坏形状计划", steps=[bad_shape])
        )
        self.assertIn("steps[0].material 需是对象", message)
        missing_id = _step()
        missing_id["material"] = {}
        message = await self.invalid_message(
            self.core.save_plan(name="缺 id 计划", steps=[missing_id])
        )
        self.assertIn("steps[0].material.material_id 必填", message)

    async def test_cases_mode_enum_and_selected_non_empty(self) -> None:
        await self.boot()
        material = await self.upload()
        binding = self.binding(material)
        message = await self.invalid_message(
            self.core.save_plan(
                name="坏 mode", steps=[_step(material=binding, cases={"mode": "bogus"})]
            )
        )
        self.assertIn("steps[0].cases.mode 非法", message)
        message = await self.invalid_message(
            self.core.save_plan(
                name="空选例", steps=[_step(material=binding, cases={"mode": "selected"})]
            )
        )
        self.assertIn("steps[0].cases.mode=selected 时 case_ids 不能为空", message)
        message = await self.invalid_message(
            self.core.save_plan(
                name="空串选例",
                steps=[_step(material=binding, cases={"mode": "selected", "case_ids": [""]})],
            )
        )
        self.assertIn("steps[0].cases.case_ids 需是非空字符串", message)

    async def test_selected_cases_injects_targets_on_dispatch(self) -> None:
        await self.boot()
        material = await self.upload()
        await self.core.put_cases(str(material["id"]), [{"case_id": "a::t1"}, {"case_id": "a::t2"}])
        plan = await self.save(
            steps=[
                _step(
                    "单测",
                    material=self.binding(material),
                    cases={"mode": "selected", "case_ids": ["a::t1"]},
                )
            ]
        )
        result = await self.core.dispatch_plan(str(plan["plan_id"]))
        detail = await self.core.get_task(str(result["task_id"]))
        assert detail is not None
        params = cast(dict[str, Any], detail["stages"][0]["params"])
        self.assertEqual(params["targets"], ["a::t1"])
        self.assertEqual(params["material_id"], str(material["id"]))

    async def test_all_mode_does_not_inject_targets(self) -> None:
        await self.boot()
        material = await self.upload()
        plan = await self.save(
            steps=[_step("单测", material=self.binding(material), cases={"mode": "all"})]
        )
        result = await self.core.dispatch_plan(str(plan["plan_id"]))
        detail = await self.core.get_task(str(result["task_id"]))
        assert detail is not None
        params = cast(dict[str, Any], detail["stages"][0]["params"])
        self.assertNotIn("targets", params)

    async def test_no_targets_injection_when_params_carry_selection(self) -> None:
        """params 显式给 selection_id 通道时不注入 targets（§2.3 缺才注入）。"""
        await self.boot()
        material = await self.upload()
        await self.core.put_cases(str(material["id"]), [{"case_id": "a::t1"}])
        sid = await self.core.save_selection(str(material["id"]), "勾选", ["a::t1"])
        plan = await self.save(
            steps=[
                _step(
                    "单测",
                    material=self.binding(material),
                    cases={"mode": "selected", "case_ids": ["a::t1"]},
                    params={"selection_id": sid},
                )
            ]
        )
        result = await self.core.dispatch_plan(str(plan["plan_id"]))
        detail = await self.core.get_task(str(result["task_id"]))
        assert detail is not None
        params = cast(dict[str, Any], detail["stages"][0]["params"])
        self.assertNotIn("targets", params)
        self.assertEqual(params["selection_id"], sid)

    async def test_legacy_step_read_compat(self) -> None:
        """旧格式步骤（顶层 material_id、无 cases、遗留 params 键）读取不炸（§2.1）。"""
        await self.boot()
        material = await self.upload()
        await self.core.put_cases(str(material["id"]), [{"case_id": "a::t1"}])
        sid = await self.core.save_selection(str(material["id"]), "勾选", ["a::t1"])
        step = _step(
            "单测",
            material_id=str(material["id"]),
            params={"targets": ["a::t1"], "selection_id": sid},
        )
        plan = await self.save(steps=[step])
        detail = await self.core.get_plan(str(plan["plan_id"]))
        assert detail is not None
        self.assertEqual(detail["steps"], [step])  # 原样存储、读取兼容
        result = await self.core.dispatch_plan(str(plan["plan_id"]))
        task = await self.core.get_task(str(result["task_id"]))
        assert task is not None
        params = cast(dict[str, Any], task["stages"][0]["params"])
        self.assertEqual(params["targets"], ["a::t1"])  # mode=all 不注入不改写
        self.assertEqual(params["selection_id"], sid)
        self.assertEqual(params["material_id"], str(material["id"]))


# ================================================================ 9. 脚本版本绑定（§3）
class PlanVersionBindingTests(PlansStackTestCase):
    """版本漂移 / 用例消失（编辑器 v2 设计 §3 表逐条）。"""

    async def test_tampered_fingerprint_rejected(self) -> None:
        await self.boot()
        material = await self.upload()
        binding = self.binding(material)
        binding["sha256"] = "0" * 64
        message = await self.invalid_message(
            self.core.save_plan(name="漂移计划", steps=[_step(material=binding)])
        )
        self.assertIn("脚本已变更（指纹不符），请重新确认用例", message)

    async def test_recipe_update_marks_saved_binding_stale(self) -> None:
        await self.boot()
        material = await self.upload()
        stale = self.binding(material)  # update 前的指纹快照
        plan = await self.save(steps=[_step(material=stale)])
        await self.core.update_recipe(str(material["id"]), "新配方")
        message = await self.invalid_message(
            self.core.save_plan(
                plan_id=str(plan["plan_id"]), name="重存", steps=[_step(material=stale)]
            )
        )
        self.assertIn("脚本已变更（指纹不符），请重新确认用例", message)
        message = await self.invalid_message(self.core.dispatch_plan(str(plan["plan_id"])))
        self.assertIn("脚本已变更（指纹不符），请重新确认用例", message)

    async def test_missing_case_reported_one_by_one(self) -> None:
        await self.boot()
        material = await self.upload()
        await self.core.put_cases(str(material["id"]), [{"case_id": "a::t1"}, {"case_id": "a::t2"}])
        message = await self.invalid_message(
            self.core.save_plan(
                name="用例消失",
                steps=[
                    _step(
                        material=self.binding(material),
                        cases={"mode": "selected", "case_ids": ["a::t1", "gone-1", "gone-2"]},
                    )
                ],
            )
        )
        self.assertIn("用例已不存在：gone-1", message)
        self.assertIn("用例已不存在：gone-2", message)
        self.assertNotIn("用例已不存在：a::t1", message)  # 存在的用例不误报

    async def test_selected_without_case_list_rejected(self) -> None:
        await self.boot()
        material = await self.upload()
        message = await self.invalid_message(
            self.core.save_plan(
                name="未分析",
                steps=[
                    _step(
                        material=self.binding(material),
                        cases={"mode": "selected", "case_ids": ["a::t1"]},
                    )
                ],
            )
        )
        self.assertIn("脚本尚无用例清单", message)

    async def test_missing_material_reports_deleted(self) -> None:
        await self.boot()
        message = await self.invalid_message(
            self.core.save_plan(
                name="悬空 v2", steps=[_step(material={"material_id": "m-nope"})]
            )
        )
        self.assertIn("steps[0] 脚本版本已删除，请重新选择脚本", message)


# ================================================================ 10. 同名冲突（§2.1）
class PlanNameConflictTests(PlansStackTestCase):
    """params 保留键与 material / cases 的同名语义冲突（沿 material_id 先例）。"""

    async def test_params_targets_conflicts_with_selected_cases(self) -> None:
        await self.boot()
        material = await self.upload()
        await self.core.put_cases(str(material["id"]), [{"case_id": "a::t1"}, {"case_id": "a::t2"}])
        message = await self.invalid_message(
            self.core.save_plan(
                name="targets 冲突",
                steps=[
                    _step(
                        material=self.binding(material),
                        cases={"mode": "selected", "case_ids": ["a::t1"]},
                        params={"targets": ["a::t2"]},
                    )
                ],
            )
        )
        self.assertIn("steps[0].cases.case_ids 与 params.targets 同名冲突", message)

    async def test_equal_targets_is_harmless_duplicate(self) -> None:
        await self.boot()
        material = await self.upload()
        await self.core.put_cases(str(material["id"]), [{"case_id": "a::t1"}])
        plan = await self.save(
            steps=[
                _step(
                    material=self.binding(material),
                    cases={"mode": "selected", "case_ids": ["a::t1"]},
                    params={"targets": ["a::t1"]},
                )
            ]
        )
        self.assertTrue(str(plan["plan_id"]).startswith("p-"))

    async def test_params_selection_conflicts_with_selected_cases(self) -> None:
        await self.boot()
        material = await self.upload()
        await self.core.put_cases(str(material["id"]), [{"case_id": "a::t1"}, {"case_id": "a::t2"}])
        sid = await self.core.save_selection(str(material["id"]), "勾选", ["a::t2"])
        message = await self.invalid_message(
            self.core.save_plan(
                name="selection 冲突",
                steps=[
                    _step(
                        material=self.binding(material),
                        cases={"mode": "selected", "case_ids": ["a::t1"]},
                        params={"selection_id": sid},
                    )
                ],
            )
        )
        self.assertIn("steps[0].cases.case_ids 与 params.selection_id 同名冲突", message)

    async def test_top_material_id_conflicts_with_material(self) -> None:
        await self.boot()
        mid_a = str((await self.upload(name="包A", data=_zip_bytes({"a.py": "x = 1\n"})))["id"])
        mid_b = str((await self.upload(name="包B", data=_zip_bytes({"b.py": "y = 2\n"})))["id"])
        message = await self.invalid_message(
            self.core.save_plan(
                name="双写材料",
                steps=[_step(material={"material_id": mid_a}, material_id=mid_b)],
            )
        )
        self.assertIn("steps[0].material_id 与 material.material_id 同名冲突", message)
        self.assertIn(f"{mid_b!r} ≠ {mid_a!r}", message)

    async def test_legacy_targets_with_mode_all_tolerated(self) -> None:
        """无 cases 的遗留 params.targets 不误伤（读取不炸，§2.1）。"""
        await self.boot()
        material = await self.upload()
        plan = await self.save(
            steps=[_step(material=self.binding(material), params={"targets": ["a::t1"]})]
        )
        self.assertTrue(str(plan["plan_id"]).startswith("p-"))


# ================================================================ 11. 台架定向调度（§4）
class SchedulerTargetingTests(unittest.TestCase):
    """_candidate_nodes allowlist 交集 + pick_node random 策略（编辑器 v2 设计 §4.2）。"""

    def _entry(self, node_id: str) -> dict[str, Any]:
        return {"kind": "executor", "id": "pytest", "node_id": node_id, "labels": {"lang": "python"}}

    def _scheduler(
        self,
        *,
        entries: Sequence[Mapping[str, Any]] | None = None,
        comm_nodes: Sequence[str] = (),
    ) -> Any:
        class _NodesStub:
            def select(
                self, kinds: object = None, labels: object = None
            ) -> list[Mapping[str, Any]]:
                return [dict(entry) for entry in (entries or ())]

        class _CommStub:
            def nodes(self) -> list[str]:
                return list(comm_nodes)

        return Scheduler(
            db=None,
            comm=_CommStub(),
            config=CoreConfig(),
            log=lambda _message: None,
            nodes=(lambda: _NodesStub()) if entries is not None else (lambda: None),
            self_name=lambda: "master",
        )

    @staticmethod
    def _first(pool: Sequence[str]) -> str:
        """随机策略打桩：取池首元素（保持可断言）。"""
        return pool[0]

    def test_nodes_allowlist_intersects_candidates(self) -> None:
        service = self._scheduler(
            entries=[self._entry("bench-a"), self._entry("bench-b"), self._entry("bench-c")]
        )
        executor = ExecutorSelector(id="pytest", nodes=("bench-a", "bench-c", "ghost"))
        self.assertEqual(service._candidate_nodes(executor), ["bench-a", "bench-c"])
        # 空 allowlist = 不指定（能力匹配全量候选）
        self.assertEqual(len(service._candidate_nodes(ExecutorSelector(id="pytest"))), 3)

    def test_allowlist_applies_to_comm_fallback(self) -> None:
        service = self._scheduler(comm_nodes=["master", "bench-a", "bench-b"])
        executor = ExecutorSelector(id="pytest", nodes=("bench-b",))
        self.assertEqual(service._candidate_nodes(executor), ["bench-b"])

    def test_random_strategy_draws_from_usable_excluding_avoid(self) -> None:
        service = self._scheduler(
            entries=[self._entry("bench-a"), self._entry("bench-b"), self._entry("bench-c")]
        )
        executor = ExecutorSelector(id="pytest", strategy="random")
        with mock.patch(
            "master_plugins.test_core.scheduler.random.choice",
            side_effect=self._first,
        ) as choice:
            node = service.pick_node(executor, inflight=InflightStats(), avoid="bench-c")
        self.assertEqual(node, "bench-a")
        # 有非 avoid 候选时先剔 avoid 再随机
        self.assertEqual(list(choice.call_args[0][0]), ["bench-a", "bench-b"])

    def test_random_strategy_falls_back_to_avoid_when_alone(self) -> None:
        service = self._scheduler(entries=[self._entry("bench-c")])
        executor = ExecutorSelector(id="pytest", strategy="random")
        with mock.patch(
            "master_plugins.test_core.scheduler.random.choice",
            side_effect=self._first,
        ) as choice:
            node = service.pick_node(executor, inflight=InflightStats(), avoid="bench-c")
        self.assertEqual(node, "bench-c")
        self.assertEqual(list(choice.call_args[0][0]), ["bench-c"])

    def test_balanced_keeps_least_inflight_first(self) -> None:
        service = self._scheduler(entries=[self._entry("bench-a"), self._entry("bench-b")])
        inflight = InflightStats()
        inflight.per_node["bench-a"] = 2
        node = service.pick_node(ExecutorSelector(id="pytest"), inflight=inflight)
        self.assertEqual(node, "bench-b")


# ================================================================ 12. trigger / notify（§5）
class TriggerNotifyTests(PlansStackTestCase):
    """trigger / notify 预留接口：落库回显 + 枚举校验（编辑器 v2 设计 §5）。"""

    async def test_defaults_to_manual_and_none(self) -> None:
        await self.boot()
        plan = await self.save()
        self.assertEqual(plan["trigger"], {"type": "manual", "config": {}})
        self.assertEqual(plan["notify"], {"type": "none", "config": {}})

    async def test_stored_and_returned_still_dispatchable(self) -> None:
        await self.boot()
        trigger = {"type": "cron", "config": {"expr": "0 0 * * *"}}
        notify = {"type": "webhook", "config": {"url": "http://x"}}
        plan = await self.save(trigger=trigger, notify=notify)
        self.assertEqual(plan["trigger"], trigger)
        self.assertEqual(plan["notify"], notify)
        result = await self.core.dispatch_plan(str(plan["plan_id"]))  # 照存照显、仍可下发
        self.assertTrue(str(result["task_id"]))

    async def test_enum_and_shape_validated(self) -> None:
        await self.boot()
        message = await self.invalid_message(
            self.core.save_plan(
                name="坏触发", steps=[_step()], trigger={"type": "bogus", "config": {}}
            )
        )
        self.assertIn("trigger.type 非法", message)
        message = await self.invalid_message(
            self.core.save_plan(name="坏通知", steps=[_step()], notify={"type": "sms", "config": {}})
        )
        self.assertIn("notify.type 非法", message)
        message = await self.invalid_message(
            self.core.save_plan(
                name="坏配置", steps=[_step()], trigger={"type": "cron", "config": "x"}
            )
        )
        self.assertIn("trigger.config 需是对象", message)
        message = await self.invalid_message(
            self.core.save_plan(
                name="坏形状", steps=[_step()], trigger="bad"  # type: ignore[arg-type]
            )
        )
        self.assertIn("trigger 需是对象", message)


# ================================================================ 13. executor.nodes / strategy 校验
class ExecutorTargetingSpecTests(PlansStackTestCase):
    """步骤 executor 台架定向字段校验与落库（编辑器 v2 设计 §8.5）。"""

    async def test_nodes_and_strategy_validated(self) -> None:
        await self.boot()
        binding = self.binding(await self.upload())
        message = await self.invalid_message(
            self.core.save_plan(name="坏 nodes", steps=[_step(material=binding, nodes="x")])
        )
        self.assertIn("steps[0].executor.nodes 需是字符串数组", message)
        message = await self.invalid_message(
            self.core.save_plan(
                name="空串台架", steps=[_step(material=binding, nodes=["bench-a", ""])]
            )
        )
        self.assertIn("steps[0].executor.nodes 含空串", message)
        message = await self.invalid_message(
            self.core.save_plan(name="坏策略", steps=[_step(material=binding, strategy="bogus")])
        )
        self.assertIn("steps[0].executor.strategy 非法", message)

    async def test_nodes_deduped_and_strategy_staged(self) -> None:
        await self.boot()
        plan = await self.save(
            steps=[
                _step(
                    material=self.binding(await self.upload()),
                    nodes=["bench-a", "bench-a", "bench-b"],
                    strategy="random",
                )
            ]
        )
        result = await self.core.dispatch_plan(str(plan["plan_id"]))
        detail = await self.core.get_task(str(result["task_id"]))
        assert detail is not None
        executor = cast(dict[str, Any], detail["stages"][0]["executor"])
        self.assertEqual(executor["nodes"], ["bench-a", "bench-b"])
        self.assertEqual(executor["strategy"], "random")
