"""test_core cron 定时触发测试（测试计划编辑器 v2 设计 §5.1）。

覆盖（分组 TestCase）：

1. cron 解析器单测（纯函数）：子集写法 ``*`` / ``*/n`` / ``a`` / ``a-b`` /
   ``a-b/n`` / 逗号列表、字段边界（周 0-6、7 亦作周日）、非法表达式逐条
   （段数 / 越界 / 步长 0 / 起点大于终点 / 写法不支持 / 空串 / 空项）；
2. 日 / 周 OR 语义：双双限定（均非 ``*``）任一命中即算，单限定走 AND；
   ``next_after`` 下一命中槽（含 2 月 29 日长空档与永不命中）；
3. save_plan 校验：cron 表达式非法 → InvalidSpec（「trigger.config.expr
   非法：<原因>」）；event / manual 不校验 expr（照存照显）；计划 dict 纯
   增量键 ``next_run_ts``（只算 cron 型）；
4. 触发循环（``_trigger_tick`` 手动驱动 + 可控时钟，仿 dispatch / sweep
   循环测试手法）：命中槽派发一次、同槽不重派、错过即跳过（无 backlog）、
   幂等键格式 ``cron:<plan_id>:<slot>``、同槽重启重派被 submit 幂等去重、
   表达式更新（updated_ts 变）按新表达式命中、非法表达式跳过且只告警一次
   （防刷屏）、派发异常仍推进游标（防刷屏）。

测试引导：复用 tests/test_test_core_plans.py 的最小栈（PlansStackTestCase），
后台触发循环挂起（_TRIGGER_INTERVAL 拉大），全部手动 ``_trigger_tick`` 驱动。
"""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from unittest import mock

from common.test_contracts import LABEL_PLAN_ID, InvalidSpec
from master_plugins.test_core import TestCorePlugin as CorePlugin
from master_plugins.test_core.cron_parse import parse_cron
from master_plugins.test_core.models import TestPlan as PlanRow
from master_plugins.test_core.models import TestTask as TaskRow
from test_test_core_plans import PlansStackTestCase


class Clock:
    """可控时间源（tests/test_test_core_plans.py 同款，独立副本免跨模块私有名）。"""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ================================================================ 1. cron 解析器
class CronParseTests(unittest.TestCase):
    """cron 解析器单测（纯函数，编辑器 v2 设计 §5.1 表达式子集）。"""

    def test_star_and_step_forms(self) -> None:
        expr = parse_cron("*/10 * * * *")
        self.assertEqual(expr.minutes, frozenset({0, 10, 20, 30, 40, 50}))
        self.assertTrue(expr.matches(datetime(2024, 3, 5, 10, 0)))
        self.assertTrue(expr.matches(datetime(2024, 3, 5, 10, 50)))
        self.assertFalse(expr.matches(datetime(2024, 3, 5, 10, 5)))
        # 时 / 日 / 月 / 周全通配：任意时刻其余段不受限
        self.assertTrue(expr.matches(datetime(2024, 3, 5, 23, 30)))

    def test_list_range_and_step_forms(self) -> None:
        expr = parse_cron("0,30 9-18 * * *")
        self.assertEqual(expr.minutes, frozenset({0, 30}))
        self.assertEqual(expr.hours, frozenset(range(9, 19)))
        self.assertTrue(expr.matches(datetime(2024, 3, 5, 9, 30)))
        self.assertFalse(expr.matches(datetime(2024, 3, 5, 8, 30)))
        weekly = parse_cron("0 0 * * 1-5")
        self.assertEqual(weekly.weekdays, frozenset({1, 2, 3, 4, 5}))
        self.assertTrue(weekly.matches(datetime(2024, 3, 4, 0, 0)))  # 周一
        self.assertFalse(weekly.matches(datetime(2024, 3, 2, 0, 0)))  # 周六
        stepped = parse_cron("0 0 * * 1-5/2")
        self.assertEqual(stepped.weekdays, frozenset({1, 3, 5}))

    def test_dow_seven_is_sunday(self) -> None:
        seven = parse_cron("0 0 * * 7")
        zero = parse_cron("0 0 * * 0")
        self.assertEqual(seven.weekdays, zero.weekdays)
        sunday = datetime(2024, 3, 3, 0, 0)  # 周日
        self.assertTrue(seven.matches(sunday))
        self.assertTrue(zero.matches(sunday))
        self.assertFalse(seven.matches(datetime(2024, 3, 4, 0, 0)))  # 周一
        span = parse_cron("0 0 * * 5-7")  # 周五 / 六 / 日（7 归一周日）
        self.assertEqual(span.weekdays, frozenset({0, 5, 6}))

    def test_dom_dow_or_when_both_restricted(self) -> None:
        expr = parse_cron("0 0 1 * 1")  # 每月 1 日**或**每周一
        self.assertTrue(expr.matches(datetime(2024, 5, 1, 0, 0)))  # 周三、1 日 → dom 命中
        self.assertTrue(expr.matches(datetime(2024, 5, 6, 0, 0)))  # 周一、6 日 → dow 命中
        self.assertTrue(expr.matches(datetime(2024, 4, 1, 0, 0)))  # 周一且 1 日 → 双命中
        self.assertFalse(expr.matches(datetime(2024, 5, 7, 0, 0)))  # 周二、7 日 → 双不中

    def test_single_restriction_ands(self) -> None:
        monthly = parse_cron("0 0 15 * *")
        self.assertTrue(monthly.matches(datetime(2024, 5, 15, 0, 0)))
        self.assertFalse(monthly.matches(datetime(2024, 5, 6, 0, 0)))  # 周一但 6 日
        weekly = parse_cron("0 0 * * 3")
        self.assertTrue(weekly.matches(datetime(2024, 5, 1, 0, 0)))  # 周三
        self.assertFalse(weekly.matches(datetime(2024, 5, 6, 0, 0)))  # 周一

    def test_invalid_expressions_raise_chinese_errors(self) -> None:
        cases = [
            ("", "表达式为空"),
            ("   ", "表达式为空"),
            ("* * * *", "段数应为 5"),
            ("* * * * * *", "段数应为 5"),
            ("61 * * * *", "值 61 超出 0-59"),
            ("* * 0 * *", "值 0 超出 1-31"),
            ("* * * 13 *", "值 13 超出 1-12"),
            ("* * * * 8", "值 8 超出 0-7"),
            ("*/0 * * * *", "步长需 ≥ 1"),
            ("1-0 * * * *", "起点大于终点"),
            ("5/2 * * * *", "写法不支持"),
            ("1,,2 * * * *", "含空项"),
            ("bogus * * * *", "非法数字"),
        ]
        for source, fragment in cases:
            with self.subTest(source=source):
                with self.assertRaises(ValueError) as ctx:
                    parse_cron(source)
                self.assertIn(fragment, str(ctx.exception))

    def test_next_after_finds_next_slot(self) -> None:
        expr = parse_cron("*/15 * * * *")
        self.assertEqual(
            expr.next_after(datetime(2024, 5, 5, 10, 7, 30)), datetime(2024, 5, 5, 10, 15)
        )
        # 恰在命中槽上：严格向后取下一个
        self.assertEqual(
            expr.next_after(datetime(2024, 5, 5, 10, 15)), datetime(2024, 5, 5, 10, 30)
        )
        leap = parse_cron("0 0 29 2 *")
        self.assertEqual(leap.next_after(datetime(2024, 3, 1)), datetime(2028, 2, 29))
        never = parse_cron("0 0 30 2 *")  # 2 月 30 日：永不命中
        self.assertIsNone(never.next_after(datetime(2024, 3, 1)))


# ================================================================ 栈共用基类
class _TriggerStackTestCase(PlansStackTestCase):
    """计划最小栈 + 触发循环挂起（_TRIGGER_INTERVAL 拉大，手动 _trigger_tick）。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self._trigger_patch = mock.patch(
            "master_plugins.test_core.plugin._TRIGGER_INTERVAL", 3600.0
        )
        self._trigger_patch.start()

    async def asyncTearDown(self) -> None:
        self._trigger_patch.stop()
        await super().asyncTearDown()

    def set_clock(self, hour: int, minute: int, second: int = 0) -> Clock:
        """核心时钟设到 2024-03-05（周二）H:M:S 本地时刻（可推进的假时钟）。"""
        clock = Clock(datetime(2024, 3, 5, hour, minute, second).timestamp())
        self.core._clock = clock
        return clock

    async def plan_tasks(self) -> list[dict[str, Any]]:
        """source="plan-dispatch" 的任务行（cron / 手动计划派发均落此域）。"""
        rows = await self.core._repo(TaskRow).list(where=TaskRow.source == "plan-dispatch")
        return [self.core._task_dict(row) for row in rows]


# ================================================================ 2. 保存校验 / 回显
class CronSaveValidationTests(_TriggerStackTestCase):
    """save_plan 的 cron 表达式校验与 next_run_ts 回显（§5.1）。"""

    async def test_invalid_cron_expr_rejected_on_save(self) -> None:
        await self.boot()
        cases: tuple[tuple[Mapping[str, Any], str], ...] = (
            ({"type": "cron", "config": {"expr": "bogus"}}, "cron 段数应为 5"),
            ({"type": "cron", "config": {}}, "需是字符串"),
            ({"type": "cron", "config": {"expr": 5}}, "需是字符串"),
            ({"type": "cron", "config": {"expr": "*/0 * * * *"}}, "步长需 ≥ 1"),
        )
        for trigger, fragment in cases:
            with self.subTest(trigger=trigger):
                message = await self.invalid_message(self.save(trigger=trigger))
                self.assertIn("trigger.config.expr 非法", message)
                self.assertIn(fragment, message)

    async def test_event_and_manual_skip_expr_check(self) -> None:
        await self.boot()
        triggers: tuple[Mapping[str, Any], ...] = (
            {"type": "event", "config": {"expr": "not a cron at all"}},
            {"type": "manual", "config": {"expr": "!!!"}},
        )
        for trigger in triggers:
            with self.subTest(trigger=trigger):
                plan = await self.save(trigger=trigger)
                self.assertEqual(plan["trigger"], trigger)  # 照存照显

    async def test_plan_dict_carries_next_run_ts_for_cron_only(self) -> None:
        await self.boot()
        self.set_clock(10, 7, 30)
        plan = await self.save(trigger={"type": "cron", "config": {"expr": "*/15 * * * *"}})
        expected = datetime(2024, 3, 5, 10, 15).timestamp()  # 下一命中槽 10:15
        self.assertEqual(plan["next_run_ts"], expected)
        detail = await self.core.get_plan(str(plan["plan_id"]))
        assert detail is not None
        self.assertEqual(detail["next_run_ts"], expected)
        manual = await self.save()
        self.assertIsNone(manual["next_run_ts"])


# ================================================================ 3. 触发循环
class CronTriggerLoopTests(_TriggerStackTestCase):
    """cron 触发循环（_trigger_tick 手动驱动 + 可控时钟，§5.1）。"""

    async def test_hit_dispatches_once_with_slot_key(self) -> None:
        await self.boot()
        clock = self.set_clock(10, 7, 30)
        plan = await self.save(trigger={"type": "cron", "config": {"expr": "8 * * * *"}})
        plan_id = str(plan["plan_id"])
        await self.core._trigger_tick()  # 槽 10:07 不命中
        self.assertEqual(await self.plan_tasks(), [])
        clock.advance(40.0)  # 10:08:10 → 槽 10:08 命中
        await self.core._trigger_tick()
        tasks = await self.plan_tasks()
        self.assertEqual(len(tasks), 1)
        slot = int(clock.now // 60) * 60
        self.assertEqual(tasks[0]["idempotency_key"], f"cron:{plan_id}:{slot}")
        self.assertEqual(tasks[0]["labels"].get(LABEL_PLAN_ID), plan_id)
        clock.advance(45.0)  # 10:08:55 同槽再 tick：不重派
        await self.core._trigger_tick()
        self.assertEqual(len(await self.plan_tasks()), 1)

    async def test_same_slot_redispatch_after_restart_is_deduped(self) -> None:
        await self.boot()
        clock = self.set_clock(10, 7, 30)
        plan = await self.save(trigger={"type": "cron", "config": {"expr": "8 * * * *"}})
        plan_id = str(plan["plan_id"])
        clock.advance(40.0)  # 10:08:10
        await self.core._trigger_tick()
        slot = int(clock.now // 60) * 60
        self.assertEqual(len(await self.plan_tasks()), 1)
        # 模拟 master 同分钟内重启：内存游标丢失，重派同 slot（服务经 traceable
        # 代理暴露，方法补丁需落在类上才传播到内部 self.* 调用点）
        self.core._cron_cursors.clear()
        original = CorePlugin.dispatch_plan
        calls: list[dict[str, Any]] = []

        async def spy(
            self: Any, pid: str, overrides: Mapping[str, Any] | None = None
        ) -> Mapping[str, Any]:
            calls.append(dict(overrides or {}))
            return await original(self, pid, overrides)

        with mock.patch.object(CorePlugin, "dispatch_plan", new=spy):
            await self.core._trigger_tick()
        self.assertEqual(len(calls), 1)  # 确实重派了
        self.assertEqual(calls[0]["idempotency_key"], f"cron:{plan_id}:{slot}")
        self.assertEqual(len(await self.plan_tasks()), 1)  # submit 幂等去重
        self.assertEqual(self.core.health_metrics()["cron_dispatch_total"], 2)  # 两次尝试

    async def test_missed_slots_are_skipped(self) -> None:
        await self.boot()
        clock = self.set_clock(10, 7, 30)
        plan = await self.save(trigger={"type": "cron", "config": {"expr": "* * * * *"}})
        plan_id = str(plan["plan_id"])
        await self.core._trigger_tick()  # 槽 10:07
        first_slot = int(clock.now // 60) * 60
        clock.now = datetime(2024, 3, 5, 10, 30, 5).timestamp()  # 直接跳到 10:30
        await self.core._trigger_tick()  # 只补当前槽，历史槽不补派
        second_slot = int(clock.now // 60) * 60
        tasks = await self.plan_tasks()
        self.assertEqual(
            {task["idempotency_key"] for task in tasks},
            {f"cron:{plan_id}:{first_slot}", f"cron:{plan_id}:{second_slot}"},
        )

    async def test_expr_update_invalidates_cache(self) -> None:
        await self.boot()
        clock = self.set_clock(10, 7, 30)
        plan = await self.save(trigger={"type": "cron", "config": {"expr": "8 * * * *"}})
        plan_id = str(plan["plan_id"])
        clock.advance(40.0)  # 10:08:10 → 旧表达式命中
        await self.core._trigger_tick()
        self.assertEqual(len(await self.plan_tasks()), 1)
        clock.advance(5.0)  # 10:08:15 → 更新表达式（updated_ts 变，缓存失效）
        await self.save(
            plan_id=plan_id, trigger={"type": "cron", "config": {"expr": "9 * * * *"}}
        )
        clock.now = datetime(2024, 3, 5, 10, 9, 10).timestamp()  # 槽 10:09 仅新表达式命中
        await self.core._trigger_tick()
        self.assertEqual(len(await self.plan_tasks()), 2)
        clock.now = datetime(2024, 3, 5, 10, 18, 10).timestamp()  # 两个表达式都不命中
        await self.core._trigger_tick()
        self.assertEqual(len(await self.plan_tasks()), 2)

    async def test_bad_expr_skipped_with_single_warning(self) -> None:
        await self.boot()
        clock = self.set_clock(10, 7, 30)
        plan = await self.save(trigger={"type": "cron", "config": {"expr": "8 * * * *"}})
        clock.advance(5.0)
        # 直接落库坏行（绕过保存校验，模拟老库坏行 / 手工改库）
        await self.core._repo(PlanRow).update(
            str(plan["plan_id"]),
            {
                "trigger_cfg": json.dumps({"type": "cron", "config": {"expr": "bad"}}),
                "updated_ts": clock.now,
            },
        )
        warns: list[str] = []

        def warn_spy(self: Any, message: str) -> None:
            warns.append(str(message))

        clock.now = datetime(2024, 3, 5, 10, 8, 10).timestamp()  # 旧表达式本可命中
        with mock.patch.object(CorePlugin, "_warn", new=warn_spy):
            await self.core._trigger_tick()
            await self.core._trigger_tick()
        self.assertEqual(await self.plan_tasks(), [])  # 非法表达式不派发
        self.assertEqual(len(warns), 1)  # 同 updated_ts 只告警一次（防刷屏）
        self.assertIn("cron 表达式非法", warns[0])

    async def test_dispatch_failure_advances_cursor(self) -> None:
        await self.boot()
        clock = self.set_clock(10, 7, 30)
        plan = await self.save(trigger={"type": "cron", "config": {"expr": "8 * * * *"}})
        plan_id = str(plan["plan_id"])
        attempts: list[str] = []

        async def failing(
            self: Any, pid: str, overrides: Mapping[str, Any] | None = None
        ) -> Mapping[str, Any]:
            attempts.append(pid)
            raise InvalidSpec("脚本版本已删除，请重新选择脚本")

        clock.advance(40.0)  # 10:08:10 → 槽 10:08 命中
        with mock.patch.object(CorePlugin, "dispatch_plan", new=failing):
            await self.core._trigger_tick()  # 派发异常不外抛
            await self.core._trigger_tick()  # 同槽不重试（游标已推进，防刷屏）
        self.assertEqual(attempts, [plan_id])
        self.assertEqual(await self.plan_tasks(), [])
        self.assertEqual(self.core.health_metrics()["cron_dispatch_total"], 0)  # 失败不计数

    async def test_cron_dispatch_counted_in_health_metrics(self) -> None:
        await self.boot()
        clock = self.set_clock(10, 7, 30)
        await self.save(trigger={"type": "cron", "config": {"expr": "8 * * * *"}})
        self.assertEqual(self.core.health_metrics()["cron_dispatch_total"], 0)
        clock.advance(40.0)
        await self.core._trigger_tick()
        self.assertEqual(self.core.health_metrics()["cron_dispatch_total"], 1)


if __name__ == "__main__":
    unittest.main()
