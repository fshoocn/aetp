"""任务状态机测试（D-22 目标命名，P3.1）。"""

from __future__ import annotations

import pytest

from master.domain.enums import TaskStatus
from master.domain.models import Task
from master.domain.state_machine import InvalidStateTransitionError


def _task(status: TaskStatus | None = None) -> Task:
    task = Task.create(task_id="T-1", project_id="p1", device_id="d1", command={}, created_by=1)
    if status is not None:
        task.status = status
    return task


def test_valid_flow_to_succeeded():
    task = _task()
    task.mark_dispatching()
    assert task.status == TaskStatus.DISPATCHING
    task.mark_running()
    assert task.status == TaskStatus.RUNNING
    assert task.started_at is not None
    task.succeed({"ok": True})
    assert task.status == TaskStatus.SUCCEEDED
    assert task.result == {"ok": True}
    assert task.error is None
    assert task.finished_at is not None
    assert task.is_terminal


def test_dispatch_exhausted_to_failed():
    task = _task()
    task.mark_dispatching()
    task.fail("dispatch exhausted")
    assert task.status == TaskStatus.FAILED
    assert task.error == "dispatch exhausted"


def test_running_to_cancelling_to_cancelled():
    task = _task()
    task.mark_dispatching()
    task.mark_running()
    task.cancel()
    assert task.status == TaskStatus.CANCELLING
    task.mark_cancelled()
    assert task.status == TaskStatus.CANCELLED
    assert task.is_terminal


def test_pending_cancel_directly():
    task = _task()
    task.cancel()
    assert task.status == TaskStatus.CANCELLED
    assert task.is_terminal


def test_dispatching_cancel_directly():
    task = _task()
    task.mark_dispatching()
    task.cancel()
    assert task.status == TaskStatus.CANCELLED


def test_running_timed_out():
    task = _task()
    task.mark_dispatching()
    task.mark_running()
    task.mark_timed_out()
    assert task.status == TaskStatus.TIMED_OUT
    assert task.finished_at is not None
    assert task.is_terminal


def test_terminal_cannot_transition():
    task = _task()
    task.mark_dispatching()
    task.mark_running()
    task.succeed()
    with pytest.raises(InvalidStateTransitionError):
        task.mark_running()
    with pytest.raises(InvalidStateTransitionError):
        task.fail("again")


def test_invalid_transitions_rejected():
    # pending 不能直接运行
    task = _task()
    with pytest.raises(InvalidStateTransitionError):
        task.mark_running()

    # dispatching 不能直接成功
    task.mark_dispatching()
    with pytest.raises(InvalidStateTransitionError):
        task.succeed()

    # 未请求取消时不能标记取消完成
    with pytest.raises(InvalidStateTransitionError):
        task.mark_cancelled()


def test_cancel_from_terminal_rejected():
    task = _task()
    task.cancel()
    with pytest.raises(InvalidStateTransitionError):
        task.cancel()
