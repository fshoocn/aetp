"""test_core 持久化数据模型（DESIGN §4，owner = ``test_core``）。

声明方式同 :mod:`common.plugins.comm.outbox`：只给逻辑表名，物理表名由
:mod:`common.models` 改写为 ``aetp_test_core_<logical>``。JSON 载荷
（labels / params / executor / shard / requires / summary）一律 ``Mapped[str]``
文本列，避免类型歧义（同 outbox 的 payload 惯例）。只有 test_core 写这些表，
消费插件各建各表、互不 import。
"""

from __future__ import annotations

from sqlalchemy import text

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
    #: 期望分片数提示（ShardSpec.hint；plan Job 的 shard 载荷透传给执行器）
    shard_hint: Mapped[int] = mapped_column(default=0)
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


class TestMaterial(Model):
    """材料（脚本包）主表（executor_pytest DESIGN §3；内容不透明，归核心存储）。

    去重键 ``(sha256, recipe_fingerprint)``（§4.3）：同内容同 recipe 字符串是
    同一材料（幂等复用），同内容不同字符串是不同测试工程；
    材料文件本体在 ``data/plugins/test_core/materials/{id}/bundle.zip``，不建
    material_file 表（D13）。
    """

    __owner__ = "test-core"
    __tablename__ = "material"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    #: 归属执行器 id（空 = 通用材料，§4.5；筛选谓词 == X OR = ''）
    executor_id: Mapped[str] = mapped_column(default="")
    kind: Mapped[str]
    size: Mapped[int]
    sha256: Mapped[str]
    #: 配方指纹（sha256(recipe.encode("utf-8")) 截 16 hex，§4.3；空串恒定指纹）
    recipe_fingerprint: Mapped[str]
    #: 执行器自定义不透明字符串（核心只存取、不解析、不校验）
    recipe: Mapped[str]
    state: Mapped[str]
    #: JSON 文本（最近一次 inspect 问题列表 [{code, path, line?, message, severity}]）
    problems: Mapped[str]
    source: Mapped[str]
    created_by: Mapped[str]
    #: JSON 文本（标签轴）
    labels: Mapped[str]
    created_ts: Mapped[float]
    updated_ts: Mapped[float]


class TestMaterialCase(Model):
    """材料用例清单（inspect / collect 解析结果；id 取 ``material_id::case_id`` 天然幂等）。"""

    __owner__ = "test-core"
    __tablename__ = "material_case"

    id: Mapped[str] = mapped_column(primary_key=True)
    material_id: Mapped[str]
    #: pytest nodeid（用例稳定标识，D2）
    case_id: Mapped[str]
    name: Mapped[str]
    path: Mapped[str]
    #: JSON 文本（marker 列表）
    markers: Mapped[str]
    #: JSON 文本（nodeid 解析的近似参数值，§7）
    params: Mapped[str]
    #: ok / error（error = 解析异常的用例，不阻塞其余入库）
    status: Mapped[str]


class TestMaterialSelection(Model):
    """选择集（任务下发 UI 勾选结果；Job 只带 selection_id 引用，D19）。"""

    __owner__ = "test-core"
    __tablename__ = "material_selection"

    id: Mapped[str] = mapped_column(primary_key=True)
    material_id: Mapped[str]
    name: Mapped[str]
    #: JSON 文本（case_id 列表）
    case_ids: Mapped[str]
    created_by: Mapped[str]
    created_ts: Mapped[float]


class TestPlan(Model):
    """测试计划（保存 / 复用 / 复制，测试计划设计 §4.1；下发 = 实例化为任务）。"""

    __owner__ = "test-core"
    __tablename__ = "plan"

    #: `_new_id("p", now)` 时间有序 id（plugin 生成）
    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    description: Mapped[str]
    #: JSON 文本（注入任务 labels）
    labels: Mapped[str]
    #: JSON 文本（{priority, failure_policy, timeout_s}）
    defaults: Mapped[str]
    #: JSON 文本（步骤数组，§4.2：StageSpec + material_id）
    steps: Mapped[str]
    #: JSON 文本（触发方式 {type, config}，编辑器 v2 设计 §5 预留接口）
    trigger_cfg: Mapped[str] = mapped_column(default="", server_default=text("''"))
    #: JSON 文本（结果通知 {type, config}，编辑器 v2 设计 §5 预留接口）
    notify_cfg: Mapped[str] = mapped_column(default="", server_default=text("''"))
    created_by: Mapped[str]
    created_ts: Mapped[float]
    updated_ts: Mapped[float]


__all__: list[str] = [
    "TestArtifact",
    "TestCaseResult",
    "TestJob",
    "TestJobResult",
    "TestMaterial",
    "TestMaterialCase",
    "TestMaterialSelection",
    "TestPlan",
    "TestStage",
    "TestTask",
    "TestTaskHistory",
]
