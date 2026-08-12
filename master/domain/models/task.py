"""领域对象：测试任务（含状态机）。

任务状态迁移规则：
    pending → dispatched → accepted → running → completed / failed
    pending → cancelled
    running → timeout

状态迁移通过 Task 自身的方法完成，非法迁移会抛出
InvalidTaskTransitionError，由应用层/API 层映射为 409。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from master.domain.enums import TaskStatus
from master.domain.time import utcnow


class InvalidTaskTransitionError(ValueError):
    """任务状态迁移非法。"""


# 允许的状态迁移表
_ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.DISPATCHED, TaskStatus.CANCELLED},
    TaskStatus.DISPATCHED: {TaskStatus.ACCEPTED, TaskStatus.CANCELLED},
    TaskStatus.ACCEPTED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMEOUT},
}

_TERMINAL_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.TIMEOUT,
}


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
    ) -> "Task":
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
        if self.status not in _ALLOWED_TRANSITIONS:
            raise InvalidTaskTransitionError(
                f"任务状态 {self.status.value} 已终态，无法迁移到 {target.value}"
            )
        if target not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTaskTransitionError(
                f"非法任务状态迁移: {self.status.value} -> {target.value}"
            )
        self.status = target
        self.updated_at = utcnow()

    def mark_dispatched(self) -> None:
        """派发给 Agent。"""
        self._transition(TaskStatus.DISPATCHED)

    def mark_accepted(self) -> None:
        """Agent 已接受。"""
        self._transition(TaskStatus.ACCEPTED)

    def mark_running(self) -> None:
        """开始执行。"""
        self._transition(TaskStatus.RUNNING)
        self.started_at = utcnow()

    def complete(self, result: dict | None = None) -> None:
        """执行成功。"""
        self._transition(TaskStatus.COMPLETED)
        self.result = result
        self.error = None
        self.finished_at = utcnow()

    def fail(self, error: str | None = None) -> None:
        """执行失败。"""
        self._transition(TaskStatus.FAILED)
        self.error = error
        self.finished_at = utcnow()

    def cancel(self) -> None:
        """取消任务。"""
        self._transition(TaskStatus.CANCELLED)
        self.finished_at = utcnow()

    def mark_timeout(self) -> None:
        """执行超时。"""
        self._transition(TaskStatus.TIMEOUT)
        self.finished_at = utcnow()

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES
