"""test-core 测试核心插件包（master_plugins/test_core）。

本包是 test-core 插件（plugin.json: id = test-core）的实现，承载测试任务的
状态机编排（:class:`.plugin.TestCorePlugin`）、调度器（:class:`.scheduler.Scheduler`）、
数据模型、状态机转移表、配置与 HookChain 宿主（DESIGN.md 为规格）。

公开导出见 ``__all__``；跨插件共用的提交面 / 下行面 / 上行面契约住
:mod:`common.test_contracts`（消费插件请从那里 import，勿经本包）。
"""

from __future__ import annotations

from .config import TestCoreConfig
from .hooks import HOOK_PHASES, HookCtx, TestHooks
from .models import (
    TestArtifact,
    TestCaseResult,
    TestJob,
    TestJobResult,
    TestStage,
    TestTask,
    TestTaskHistory,
)
from .plugin import TestCorePlugin
from .registry import (
    JOB_FINISHED,
    JOB_TERMINAL,
    JOB_TRANSITIONS,
    TASK_FINISHED,
    TASK_TERMINAL,
    TASK_TRANSITIONS,
    can_transition,
    ensure_valid,
    history_row,
    transition,
    validate_task_spec,
)
from .scheduler import MODEL_OWNER, Scheduler

# 公开面（isort 序；跨插件契约请 import common.test_contracts）
__all__: list[str] = [
    "HOOK_PHASES",
    "JOB_FINISHED",
    "JOB_TERMINAL",
    "JOB_TRANSITIONS",
    "MODEL_OWNER",
    "TASK_FINISHED",
    "TASK_TERMINAL",
    "TASK_TRANSITIONS",
    "HookCtx",
    "Scheduler",
    "TestArtifact",
    "TestCaseResult",
    "TestCoreConfig",
    "TestCorePlugin",
    "TestHooks",
    "TestJob",
    "TestJobResult",
    "TestStage",
    "TestTask",
    "TestTaskHistory",
    "can_transition",
    "ensure_valid",
    "history_row",
    "transition",
    "validate_task_spec",
]
