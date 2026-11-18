"""test_core 持久化数据模型（DESIGN §4，owner = ``test_core``）。

声明方式同 :mod:`common.plugins.comm.outbox`：只给逻辑表名，物理表名由
:mod:`common.models` 改写为 ``aetp_test_core_<logical>``。JSON 载荷
（labels / params / executor / shard / requires / summary）一律 ``Mapped[str]``
文本列，避免类型歧义（同 outbox 的 payload 惯例）。只有 test_core 写这些表，
消费插件各建各表、互不 import。
"""

from __future__ import annotations

from common.models import Mapped, Model, mapped_column


class TestTask(Model):
    """测试任务主表（提交入口，状态机见 DESIGN §5.1）。"""

    __owner__ = "test-core"
    __tablename__ = "task"

    id: Mapped[str] = mapped_column(primary_key=True)
    #: 提交去重键（非空时 (source, idempotency_key) 唯一；缺省空串）
    idempotency_key: Mapped[str] = mapped_column(default="")
    source: Mapped[str]
    name: Mapped[str]
    created_by: Mapped[str]
    priority: Mapped[int]
    #: JSON 文本（项目/租户轴标签）
    labels: Mapped[str]
    #: JSON 文本（任务级参数）
    params: Mapped[str]
    state: Mapped[str]
    current_stage: Mapped[str]
    failure_policy: Mapped[str]
    error: Mapped[str]
    created_ts: Mapped[float]
    updated_ts: Mapped[float]
    finished_ts: Mapped[float]


class TestStage(Model):
    """任务阶段（v1 线性顺序执行 + 屏障，见 DESIGN §5.1）。"""

    __owner__ = "test-core"
    __tablename__ = "stage"

    id: Mapped[str] = mapped_column(primary_key=True)
    task_id: Mapped[str]
    seq: Mapped[int]
    name: Mapped[str]
    kind: Mapped[str]
    #: JSON 文本（ExecutorSelector 执行器选择器）
    executor: Mapped[str]
    #: JSON 文本（执行器自有参数，核心不解析）
    params: Mapped[str]
    shard_mode: Mapped[str]
    #: JSON 文本（资源需求，如 {"canoe": 1}）
    requires: Mapped[str]
    state: Mapped[str]
    timeout_s: Mapped[float]


class TestJob(Model):
    """子运行/分片：调度最小单位（状态机见 DESIGN §5.2）。"""

    __owner__ = "test-core"
    __tablename__ = "job"

    id: Mapped[str] = mapped_column(primary_key=True)
    stage_id: Mapped[str]
    task_id: Mapped[str]
    node_id: Mapped[str]
    attempt: Mapped[int]
    state: Mapped[str]
    #: JSON 文本（分片描述 {index, total, unit_refs}，null=不分片）
    shard: Mapped[str]
    lease_ts: Mapped[float]
    started_ts: Mapped[float]
    finished_ts: Mapped[float]
    error: Mapped[str]
    result_seq: Mapped[int]


class TestJobResult(Model):
    """单 Job 一次 attempt 的结果（id 取 f"{job_id}:{attempt}" 天然幂等键）。"""

    __owner__ = "test-core"
    __tablename__ = "job_result"

    id: Mapped[str] = mapped_column(primary_key=True)
    job_id: Mapped[str]
    task_id: Mapped[str]
    attempt: Mapped[int]
    status: Mapped[str]
    #: JSON 文本（执行器自定摘要 {total, passed, failed, ...}）
    summary: Mapped[str]
    started_ts: Mapped[float]
    finished_ts: Mapped[float]
    error: Mapped[str]
    received_ts: Mapped[float]


class TestCaseResult(Model):
    """用例结果（超 case_results_limit 截断存，全量进报告产物）。"""

    __owner__ = "test-core"
    __tablename__ = "case_result"

    id: Mapped[str] = mapped_column(primary_key=True)
    job_id: Mapped[str]
    task_id: Mapped[str]
    attempt: Mapped[int]
    case_id: Mapped[str]
    status: Mapped[str]
    duration_s: Mapped[float]
    message: Mapped[str]
    detail_ref: Mapped[str]


class TestArtifact(Model):
    """产物引用表（只登记引用不删文件；state: local/uploaded/removed 单向）。"""

    __owner__ = "test-core"
    __tablename__ = "artifact"

    id: Mapped[str] = mapped_column(primary_key=True)
    job_id: Mapped[str]
    task_id: Mapped[str]
    kind: Mapped[str]
    name: Mapped[str]
    uri: Mapped[str]
    size: Mapped[int]
    sha256: Mapped[str]
    state: Mapped[str]
    created_ts: Mapped[float]


class TestTaskHistory(Model):
    """任务状态迁移审计（管理页时间线，见 DESIGN §5 / §14）。"""

    __owner__ = "test-core"
    __tablename__ = "task_history"

    id: Mapped[str] = mapped_column(primary_key=True)
    task_id: Mapped[str]
    from_state: Mapped[str]
    to_state: Mapped[str]
    reason: Mapped[str]
    ts: Mapped[float]


__all__: list[str] = [
    "TestArtifact",
    "TestCaseResult",
    "TestJob",
    "TestJobResult",
    "TestStage",
    "TestTask",
    "TestTaskHistory",
]
