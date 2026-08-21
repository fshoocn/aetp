"""领域对象：测试任务（含状态机）。

任务状态迁移规则（D-22 目标命名）：
    pending → dispatching → running → succeeded / failed / timed_out
    pending → cancelled
    dispatching → failed（派发耗尽）
    running → cancelling → cancelled

状态迁移通过 Task 自身的方法完成，非法迁移会抛出
InvalidStateTransitionError，由应用层/API 层映射为 409。

迁移表与校验逻辑集中在 domain/state_machine.py（纯函数，P3.6）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from master.domain.enums import TaskStatus
from master.domain.state_machine import (
    InvalidStateTransitionError,
    assert_transition,
)
from master.domain.state_machine import (
    is_terminal as _is_terminal,
)
from master.domain.time import utcnow


@dataclass
class Task:
    """测试任务。command/result 为结构化 JSON 对象。"""

    id: int | None = None
    task_id: str = ""
    project_id: str = ""
    device_id: str = ""
    status: TaskStatus = TaskStatus.PENDING
    command: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    created_by: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        project_id: str,
        device_id: str,
        command: dict,
        created_by: int,
    ) -> Task:
        """创建 pending 任务。"""
        now = utcnow()
        return cls(
            task_id=task_id,
            project_id=project_id,
            device_id=device_id,
            status=TaskStatus.PENDING,
            command=dict(command or {}),
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )

    def _transition(self, target: TaskStatus) -> None:
        assert_transition(self.status, target)
        self.status = target
        self.updated_at = utcnow()

    def mark_dispatching(self) -> None:
        """派发给 Agent（含旧 dispatched/accepted 语义）。"""
        self._transition(TaskStatus.DISPATCHING)

    def mark_running(self) -> None:
        """Agent 开始执行。"""
        self._transition(TaskStatus.RUNNING)
        self.started_at = utcnow()

    def mark_cancelling(self) -> None:
        """执行中收到取消请求，进入取消中状态。"""
        self._transition(TaskStatus.CANCELLING)

    def succeed(self, result: dict | None = None) -> None:
        """执行成功。"""
        self._transition(TaskStatus.SUCCEEDED)
        self.result = result
        self.error = None
        self.finished_at = utcnow()

    def fail(self, error: str | None = None) -> None:
        """执行失败。"""
        self._transition(TaskStatus.FAILED)
        self.error = error
        self.finished_at = utcnow()

    def cancel(self) -> None:
        """取消任务。

        pending/dispatching 直接进入 cancelled；
        running 先进入 cancelling（由 Agent 确认后 mark_cancelled）；
        cancelling 直接进入 cancelled。
        """
        if self.status in (TaskStatus.PENDING, TaskStatus.DISPATCHING):
            self._transition(TaskStatus.CANCELLED)
        elif self.status == TaskStatus.RUNNING:
            self._transition(TaskStatus.CANCELLING)
        elif self.status == TaskStatus.CANCELLING:
            self._transition(TaskStatus.CANCELLED)
        else:
            raise InvalidStateTransitionError(f"任务状态 {self.status.value} 不允许取消")
        self.finished_at = utcnow()

    def mark_cancelled(self) -> None:
        """Agent 确认取消完成（仅 cancelling 可进入）。"""
        if self.status != TaskStatus.CANCELLING:
            raise InvalidStateTransitionError(f"任务状态 {self.status.value} 未处于取消中，无法标记取消完成")
        self._transition(TaskStatus.CANCELLED)
        self.finished_at = utcnow()

    def mark_timed_out(self) -> None:
        """执行超时。"""
        self._transition(TaskStatus.TIMED_OUT)
        self.finished_at = utcnow()

    @property
    def is_terminal(self) -> bool:
        return _is_terminal(self.status)
