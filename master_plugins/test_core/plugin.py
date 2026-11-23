"""test_core 核心插件：状态机编排 + ``ctx.test_core`` 服务（DESIGN §5/§6/§10）。

职责：测试任务的**状态机唯一写者**——提交 / 拆分 / 派发编排、Stage 屏障、
结果聚合、租约与重试、取消 / 重跑、查询门面与 HookChain 宿主。跨节点交互
一律经 ``ctx.comm``（topic 见 :mod:`common.test_contracts`），事件一律
``ctx.parallel`` 派发（``common/event_contract.py`` 全 parallel，异常隔离）。
管理 API / 页面拆附属服务 :class:`.api_service.TestCoreApiService` /
:class:`.ui_service.TestCoreUiService`，webapi / webui 缺席不阻塞核心。

调度决策（队列 / 窗口 / 配额 / 选节点 / 租约查询 / comm 收发）在
:mod:`.scheduler`，本模块负责编排与落库；插件包内不 import 其他插件包，
共享契约只住 ``common/``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import shutil
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

from cordis_port import Context, EventDispatchError, Fiber, Service

from common.hook_chain import OnError
from common.test_contracts import (
    CHAIN_ARTIFACT_SINK,
    CHAIN_RESULT_TRANSFORM,
    CHAIN_TASK_TRANSFORM,
    LABEL_PLAN_ID,
    LABEL_PLAN_NAME,
    SCHEMA_VERSION,
    TEST_PROTO,
    TOPIC_JOB_LOG,
    TOPIC_JOB_PROGRESS,
    TOPIC_JOB_RESULT,
    ArtifactRef,
    CaseResult,
    ExecutorSelector,
    InvalidSpec,
    InvalidTransition,
    JobDescriptor,
    JobResult,
    LogChunk,
    MaterialInUse,
    MaterialNotFound,
    PlanNotFound,
    ShardSpec,
    StageSpec,
    TaskNotFound,
    TaskSpec,
    TestCoreInterface,
)

from .api_service import TestCoreApiService
from .config import TestCoreConfig
from .cron_parse import CronExpr, parse_cron
from .hooks import HookCtx, TestHooks
from .models import (
    TestArtifact,
    TestCaseResult,
    TestJob,
    TestJobResult,
    TestMaterial,
    TestMaterialCase,
    TestMaterialSelection,
    TestPlan,
    TestStage,
    TestTask,
    TestTaskHistory,
)
from .registry import (
    JOB_FINISHED,
    TASK_FINISHED,
    ensure_valid,
    history_row,
    transition,
    validate_task_spec,
)
from .scheduler import MODEL_OWNER, TASK_TIMEOUT_KEY, Scheduler, project_of
from .ui_service import TestCoreUiService

# -- 事件派发名（common/event_contract.py 已登记，模式全 parallel） -------------
# tests/test_event_contract.py 的扫描器只认「字面量 / 本模块级字符串常量」，
# 从 common.test_contracts import 进来的名字解析不到派发点，故以字面量重建
# （与契约层常量同值；改名先改 common/event_contract.py 注册表）。
EVENT_TASK_CREATED = "test/task-created"
EVENT_TASK_STATE = "test/task-state"
EVENT_JOB_FINISHED = "test/job-finished"
EVENT_CASE_FINISHED = "test/case-finished"
EVENT_TASK_FINISHED = "test/task-finished"
EVENT_ARTIFACT_READY = "test/artifact-ready"
EVENT_COLLECT_TASK = "test/collect-task"
EVENT_JOB_LOG = "test/job-log"
EVENT_MATERIAL_STATE = "test/material-state"

#: heartbeat/node-status 事件名（emit 模式，监听器必须同步）
_NODE_STATUS_EVENT = "heartbeat/node-status"

#: 调度循环周期（秒）：派发窗口是轻量 DB 查询，1s 粒度足够
_DISPATCH_INTERVAL: float = 1.0
#: cron 触发扫描周期（秒，测试计划编辑器 v2 设计 §5.1：tick ≈ 15s，错过即跳过）
_TRIGGER_INTERVAL: float = 15.0
#: case-finished 事件里失败明细的载荷上限（载荷 = 引用 + 摘要，禁止大对象）
_CASE_EVENT_LIMIT: int = 200
#: test/job-log 转发事件里 lines 的行数上限（载荷恒小，§10.4）
_LOG_EVENT_LIMIT: int = 3

#: JobResult.status → Job 状态（timeout 归为基础设施失败，§5.2 无 timeout 目标态）
_JOB_RESULT_STATES: dict[str, str] = {
    "succeeded": "succeeded",
    "failed": "failed",
    "canceled": "canceled",
    "timeout": "failed",
}

#: dispatch_plan overrides 白名单（测试计划设计 §5.5；未知键 → InvalidSpec 逐条）
_PLAN_OVERRIDE_KEYS: frozenset[str] = frozenset(
    {"name", "priority", "labels", "env", "stage_params", "idempotency_key"}
)
#: 环境变量保留前缀（AETP_* 禁止经 overrides 注入，评审 #9；执行端纵深保留）
_ENV_RESERVED_PREFIX = "AETP_"


def _new_id(prefix: str, now: float) -> str:
    """时间有序 id（``t-``/``j-``/``s-``/``h-``/``c-``/``a-`` 前缀，ULID 风格）。"""
    return f"{prefix}-{int(now * 1000):013d}-{secrets.token_hex(3)}"


def _json_dumps(value: object) -> str:
    """JSON 列写入（文本列，同 outbox payload 惯例）。"""
    return json.dumps(value, ensure_ascii=False)


def _decode_json(raw: object, default: Any = None) -> Any:
    """JSON 列读出；坏行 / 空值降级 default。已是对象（消息载荷）原样返回。"""
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _decode_dict(raw: object) -> dict[str, Any]:
    """JSON 列读出为 dict（坏行降级空对象）。"""
    data = _decode_json(raw, {})
    return dict(data) if isinstance(data, dict) else {}


def _public_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """剥离内部保留键（timeout 暂存等）后的对外参数。"""
    return {key: value for key, value in params.items() if key != TASK_TIMEOUT_KEY}


def _decode_list(raw: object) -> list[Any]:
    """JSON 列读出为 list（坏行降级空列表）。"""
    data = _decode_json(raw, [])
    return list(data) if isinstance(data, list) else []


def _base_dir(ctx: Context) -> Path:
    """节点目录（config.ini 所在目录）：相对路径的解析基准（同 db/log）。"""
    loaded = ctx.appconfig.path_loaded
    return loaded.parent if loaded is not None else Path.cwd()


def _recipe_fingerprint(recipe: str) -> str:
    """配方指纹 = sha256(recipe 的 utf-8 字节) 截 16 hex（§4.3）。

    recipe 是执行器自定义不透明字符串（core 不解析），指纹只看字节序列；
    空串合法，恒定指纹（sha256(b"")）。
    """
    return hashlib.sha256(recipe.encode("utf-8")).hexdigest()[:16]


def _ingest_meta(meta: object, ctx: Context) -> Mapping[str, Any]:
    """``log.ingest_remote`` 的 meta：优先沿用 comm 收件 meta（node = 发送节点，
    按 node 分组、溯源到真实输出节点），缺 node 时合成（node_name 或 "master"）。"""
    if isinstance(meta, Mapping) and meta.get("node"):
        return meta
    return {
        "node": str(ctx.get("node_name") or "master"),
        "topic": "log.batch",
        "ts": time.time(),
        "received_ts": time.time(),
        "id": "",
    }


def _log_line(raw: object) -> dict[str, object] | None:
    """日志行归一化为 {ts, level, msg}；坏行返回 None。"""
    if not isinstance(raw, Mapping):
        return None
    try:
        ts = float(raw.get("ts") or 0.0)
        level = int(raw.get("level") or 0)
    except (TypeError, ValueError):
        return None
    return {"ts": ts, "level": level, "msg": str(raw.get("msg") or "")}


def _parse_log_chunk(raw: object) -> LogChunk | None:
    """``test.job.log`` 载荷 → LogChunk（忽略未知字段、缺省给默认值，§3.2）。

    proto 不匹配拒收（D8）、缺 job_id/attempt/seq 或 stream 非法返回 None。
    """
    if not isinstance(raw, Mapping):
        return None
    job_id = str(raw.get("job_id") or "")
    stream = str(raw.get("stream") or "")
    if not job_id or stream not in ("stdout", "stderr", "case"):
        return None
    try:
        proto = int(raw.get("proto") or 0)
        attempt = int(raw.get("attempt") or 0)
        seq = int(raw.get("seq", -1))
    except (TypeError, ValueError):
        return None
    if proto != TEST_PROTO or attempt < 1 or seq < 0:
        return None
    lines: list[Mapping[str, Any]] = []
    for item in raw.get("lines") or []:
        line = _log_line(item)
        if line is not None:
            lines.append(line)
    return LogChunk(
        proto=proto,
        job_id=job_id,
        attempt=attempt,
        seq=seq,
        stream=cast("Literal['stdout', 'stderr', 'case']", stream),
        case_id=str(raw.get("case_id") or ""),
        lines=lines,
    )


def _as_mapping(value: object) -> dict[str, Any]:
    """值 → dict[str, Any]（非对象降级空对象；显式转换避免推断出 Unknown 键值）。"""
    return dict(cast("Mapping[str, Any]", value)) if isinstance(value, Mapping) else {}


def _is_mapping(value: Any) -> bool:
    """值是否为对象（独立函数边界：避免 isinstance 把 Any 窄化成 Mapping[Unknown, …]）。"""
    return isinstance(value, Mapping)


def _plan_step_params(step: Mapping[str, Any]) -> dict[str, Any]:
    """步骤 params → dict（缺失 / 非对象降级空对象）。"""
    return _as_mapping(step.get("params"))


#: 计划步骤 cases.mode 枚举（编辑器 v2 设计 §2.1）
_PLAN_CASE_MODES: tuple[str, ...] = ("all", "selected")
#: trigger.type / notify.type 枚举（编辑器 v2 设计 §5：cron 已实现，event 预留）
_TRIGGER_TYPES: tuple[str, ...] = ("manual", "cron", "event")
_NOTIFY_TYPES: tuple[str, ...] = ("none", "jira", "webhook", "email")


def _plan_material_of(step: Mapping[str, Any]) -> dict[str, Any]:
    """步骤材料视图（读取归一，编辑器 v2 设计 §2.1）。

    ``material`` 对象优先；旧顶层 ``material_id`` 归一为 ``{"material_id": …}``
    （无绑定指纹 → 仅做存在性校验，编辑器提示「请重新确认用例」）；
    都缺返回空对象（必填问题由校验链报）。
    """
    raw = step.get("material")
    if _is_mapping(raw):
        return _as_mapping(raw)
    top = step.get("material_id")
    if isinstance(top, str) and top:
        return {"material_id": top}
    return {}


def _plan_cases_of(step: Mapping[str, Any]) -> dict[str, Any]:
    """步骤用例视图（读取归一，编辑器 v2 设计 §2.1）：缺失 ``cases`` 归一为
    ``{"mode": "all", "case_ids": []}``（跟随脚本全部用例）；坏形原样保留，
    由校验链逐条报。
    """
    raw = step.get("cases")
    cases = _as_mapping(raw) if _is_mapping(raw) else {}
    mode = cases.get("mode", "all")
    case_ids = cases.get("case_ids", [])
    return {
        "mode": mode,
        "case_ids": list(case_ids) if isinstance(case_ids, list) else case_ids,
    }


def _dedupe_keep_order(items: Any) -> Any:
    """字符串数组去重保序（executor.nodes 归一，§8.5）；非数组原样返回交校验链报。"""
    if isinstance(items, (str, bytes)) or not isinstance(items, list):
        return items
    out: list[Any] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _same_str_list(left: Any, right: Any) -> bool:
    """两个列表是否同为字符串数组且同集（顺序无关；同名冲突比较用）。"""
    if isinstance(left, (str, bytes)) or not isinstance(left, list):
        return False
    if isinstance(right, (str, bytes)) or not isinstance(right, list):
        return False
    return sorted(str(item) for item in left) == sorted(str(item) for item in right)


def _check_trigger_notify(trigger: Any, notify: Any) -> list[str]:
    """trigger / notify 形状与枚举校验（编辑器 v2 设计 §5/§8.7，纯函数）。

    缺省（None）放行；``type`` 缺省补 ``manual`` / ``none``，其余类型照存照显
    （UI 标「预留」）；返回问题清单（空 = 合法）。
    """
    problems: list[str] = []
    for key, value, allowed, fallback in (
        ("trigger", trigger, _TRIGGER_TYPES, "manual"),
        ("notify", notify, _NOTIFY_TYPES, "none"),
    ):
        if value is None:
            continue
        if not _is_mapping(value):
            problems.append(f"{key} 需是对象，收到 {value!r}")
            continue
        data = _as_mapping(value)
        type_raw: Any = data.get("type", fallback)
        if not isinstance(type_raw, str) or type_raw not in allowed:
            problems.append(
                f"{key}.type 非法，收到 {type_raw!r}（可选：{' / '.join(allowed)}）"
            )
        config_raw: Any = data.get("config", {})
        if not _is_mapping(config_raw):
            problems.append(f"{key}.config 需是对象，收到 {config_raw!r}")
    return problems


def _trigger_dict(value: Mapping[str, Any] | None, default_type: str) -> dict[str, Any]:
    """trigger / notify 归一形态（落库与对外 dict 共用）：``{type, config}``。"""
    data = _as_mapping(value) if value is not None else {}
    return {
        "type": str(data.get("type") or default_type),
        "config": _as_mapping(data.get("config")),
    }


def _check_cron_expr(trigger: Any) -> list[str]:
    """cron 表达式可解析校验（编辑器 v2 设计 §5.1，保存时；纯函数）。

    ``type=="cron"`` 时校验 ``config.expr`` 可被 :func:`parse_cron` 解析，
    非法 → 逐条「trigger.config.expr 非法：<原因>」；event / manual 不校验
    expr（照存照显）；形状问题（trigger 非对象 / config 非对象）由
    :func:`_check_trigger_notify` 报，此处不重复。
    """
    if not _is_mapping(trigger):
        return []
    data = _as_mapping(trigger)
    if str(data.get("type") or "manual") != "cron":
        return []
    config_raw: Any = data.get("config", {})
    if not _is_mapping(config_raw):
        return []
    expr_raw: Any = _as_mapping(config_raw).get("expr")
    if not isinstance(expr_raw, str):
        return [f"trigger.config.expr 非法：需是字符串，收到 {expr_raw!r}"]
    try:
        parse_cron(expr_raw)
    except ValueError as exc:
        return [f"trigger.config.expr 非法：{exc}"]
    return []


def _plan_stage_specs(steps: Sequence[Mapping[str, Any]]) -> list[StageSpec]:
    """计划步骤 → StageSpec（编辑器 v2 设计 §2.1/§2.3 转换，调用方先过
    :func:`_check_plan_steps`）。

    * ``executor.kind`` 缺省补 ``"executor"``；``executor.nodes`` 去重保序、
      ``strategy`` 缺省 ``"balanced"``（台架定向，设计 §4）；
    * ``material.material_id``（旧顶层 ``material_id`` 归一同值）缺省并入
      ``params.material_id``（同名冲突在校验链已拒，评审 #5）；
    * ``cases.mode == "selected"`` 且 params 未显式给 targets/selection_id 时
      注入 ``params.targets = cases.case_ids``（§2.3）；``mode == "all"`` 不注入
      （执行端回落全量）。
    """
    stages: list[StageSpec] = []
    for step in steps:
        params = _plan_step_params(step)
        material = _plan_material_of(step)
        material_id: Any = material.get("material_id")
        if isinstance(material_id, str) and material_id and not params.get("material_id"):
            params["material_id"] = material_id
        cases = _plan_cases_of(step)
        if (
            cases.get("mode") == "selected"
            and "targets" not in params
            and "selection_id" not in params
        ):
            case_ids = cases.get("case_ids")
            if isinstance(case_ids, list):
                params["targets"] = list(case_ids)
        executor = _as_mapping(step.get("executor"))
        shard = _as_mapping(step.get("shard"))
        raw_name: Any = step.get("name")
        raw_executor_id: Any = executor.get("id")
        raw_kind: Any = executor.get("kind")
        raw_timeout: Any = step.get("timeout_s", 0.0)
        stages.append(
            StageSpec(
                name=raw_name,
                executor=ExecutorSelector(
                    id=raw_executor_id,
                    kind=str(raw_kind or "executor"),
                    labels=_as_mapping(executor.get("labels")),
                    nodes=_dedupe_keep_order(executor.get("nodes", ())),
                    strategy=executor.get("strategy", "balanced"),
                ),
                params=params,
                shard=ShardSpec(mode=shard.get("mode", "none"), hint=shard.get("hint", 0)),
                requires=_as_mapping(step.get("requires")),
                timeout_s=raw_timeout,
            )
        )
    return stages


def _check_plan_steps(steps: Sequence[Mapping[str, Any]]) -> list[str]:
    """计划步骤静态校验（编辑器 v2 设计 §2.1/§8，纯函数）：返回问题清单（空 = 合法）。

    口径复用 :func:`registry.validate_task_spec`（归一化为 TaskSpec 后整体校验
    步骤 name 非空且唯一 / executor.id / executor.nodes / strategy / shard /
    timeout_s，消息路径改写 ``steps[i]``）；另加 shape 与同名语义冲突检查：

    * ``material`` 保存必填（旧顶层 ``material_id`` 等价，读取兼容 §2.1）；
    * ``cases.mode`` 枚举、``selected`` 时 case_ids 非空字符串数组；
    * 同名语义冲突（§2.1，沿 material_id 冲突先例逐条报）：``params.material_id``
      ↔ ``material``、顶层 ``material_id`` ↔ ``material`` 值不等即冲突；
      ``params.targets`` ↔ ``cases.case_ids`` 仅 ``mode=selected`` 且值不等冲突
      （``mode=all`` 容忍遗留 targets/selection_id，读取不炸）。
    材料悬空 / 指纹漂移 / 用例消失 / selection 交叉是异步查询，在
    :meth:`TestCorePlugin._check_plan_materials` 内联，问题口径相同。
    """
    items = list(steps)
    if not items:
        return ["steps 不能为空"]
    problems: list[str] = []
    for index, step in enumerate(items):
        where = f"steps[{index}]"
        params = _plan_step_params(step)
        params_raw: Any = step.get("params")
        if params_raw is not None and not _is_mapping(params_raw):
            problems.append(f"{where}.params 需是对象，收到 {params_raw!r}")
        executor_raw: Any = step.get("executor")
        if executor_raw is not None and not _is_mapping(executor_raw):
            problems.append(f"{where}.executor 需是对象，收到 {executor_raw!r}")
        else:
            labels_raw: Any = _as_mapping(executor_raw).get("labels")
            if labels_raw is not None and not _is_mapping(labels_raw):
                problems.append(f"{where}.executor.labels 需是对象，收到 {labels_raw!r}")
        shard_raw: Any = step.get("shard")
        if shard_raw is not None and not _is_mapping(shard_raw):
            problems.append(f"{where}.shard 需是对象，收到 {shard_raw!r}")
        requires_raw: Any = step.get("requires")
        if requires_raw is not None and not _is_mapping(requires_raw):
            problems.append(f"{where}.requires 需是对象，收到 {requires_raw!r}")

        # -- material（§2.1 保存必填；旧顶层 material_id 等价归一）
        material_raw: Any = step.get("material")
        if material_raw is not None and not _is_mapping(material_raw):
            problems.append(f"{where}.material 需是对象，收到 {material_raw!r}")
        material = _plan_material_of(step)
        material_id_raw: Any = material.get("material_id")
        material_id = material_id_raw if isinstance(material_id_raw, str) else ""
        top_material: Any = step.get("material_id")
        if top_material is not None and top_material != "" and not isinstance(top_material, str):
            problems.append(f"{where}.material_id 需是字符串，收到 {top_material!r}")
        if material_id_raw is not None and material_id_raw != "" and not isinstance(material_id_raw, str):
            problems.append(f"{where}.material.material_id 需是字符串，收到 {material_id_raw!r}")
        if (
            isinstance(top_material, str)
            and top_material
            and isinstance(material_id_raw, str)
            and material_id_raw
            and top_material != material_id_raw
        ):
            problems.append(
                f"{where}.material_id 与 material.material_id 同名冲突："
                f"{top_material!r} ≠ {material_id_raw!r}"
            )
        if not material_id:
            if material_raw is None and not top_material:
                problems.append(f"{where}.material 必填（计划步骤必须绑定脚本）")
            else:
                problems.append(f"{where}.material.material_id 必填")
        inner_material: Any = params.get("material_id")
        if (
            material_id
            and isinstance(inner_material, str)
            and inner_material
            and material_id != inner_material
        ):
            problems.append(
                f"{where}.material_id 与 params.material_id 同名冲突："
                f"{material_id!r} ≠ {inner_material!r}"
            )

        # -- cases（§2.1：缺省 {mode: all}；selected 时 case_ids 非空字符串数组）
        cases_raw: Any = step.get("cases")
        if cases_raw is not None and not _is_mapping(cases_raw):
            problems.append(f"{where}.cases 需是对象，收到 {cases_raw!r}")
        cases = _plan_cases_of(step)
        mode: Any = cases.get("mode")
        case_ids_raw: Any = cases.get("case_ids")
        if mode not in _PLAN_CASE_MODES:
            problems.append(
                f"{where}.cases.mode 非法，收到 {mode!r}（可选：all / selected）"
            )
        if isinstance(case_ids_raw, (str, bytes)) or not isinstance(case_ids_raw, list):
            problems.append(f"{where}.cases.case_ids 需是字符串数组，收到 {case_ids_raw!r}")
        else:
            for item in case_ids_raw:
                if not isinstance(item, str) or not item:
                    problems.append(f"{where}.cases.case_ids 需是非空字符串，收到 {item!r}")
        if mode == "selected" and (not isinstance(case_ids_raw, list) or not case_ids_raw):
            problems.append(f"{where}.cases.mode=selected 时 case_ids 不能为空")

        # -- params.targets ↔ cases.case_ids 同名冲突（显式 cases + selected 且值不等）
        targets_raw: Any = params.get("targets")
        if (
            cases_raw is not None
            and mode == "selected"
            and targets_raw is not None
            and isinstance(case_ids_raw, list)
            and not _same_str_list(targets_raw, case_ids_raw)
        ):
            problems.append(
                f"{where}.cases.case_ids 与 params.targets 同名冲突："
                f"{case_ids_raw!r} ≠ {targets_raw!r}"
            )
    spec = TaskSpec(name="plan", stages=_plan_stage_specs(items))
    for problem in validate_task_spec(spec):
        problems.append(
            problem.replace("stages[", "steps[").replace("stages 不能为空", "steps 不能为空")
        )
    return problems


class TestCorePlugin(Service[TestCoreConfig]):
    """test-core 测试核心插件（``provide = "test_core"``，DESIGN §6）。

    ``inject`` 只声明硬依赖 appconfig / db / comm；``ctx.nodes`` 是**可选依赖**
    （从节点 / 无心跳部署缺席），访问判空、缺席时调度降级（见 Scheduler）。
    webapi / webui 不进核心依赖：管理 API / 页面拆附属服务挂载，缺席安静
    PENDING（同 heartbeat-server 模式）。
    """

    name: ClassVar[str] = "test-core"
    provide: str | None = "test_core"
    inject: ClassVar[list[str]] = ["appconfig", "db", "comm"]
    Config: ClassVar[type[TestCoreConfig]] = TestCoreConfig

    def __init__(self, ctx: Context, config: TestCoreConfig | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.test_core
        self.ctx: Context = ctx
        self.config: TestCoreConfig = (
            config if isinstance(config, TestCoreConfig) else TestCoreConfig()
        )
        #: 时钟（可注入：测试整体替换本属性，Scheduler 经 callable 晚绑定）
        self._clock: Callable[[], float] = time.time
        self._hooks: TestHooks = TestHooks(logger=ctx.logger)
        self._scheduler: Scheduler = Scheduler(
            db=ctx.db,
            comm=ctx.comm,
            config=self.config,
            clock=lambda: self._clock(),
            log=self._warn,
            nodes=lambda: self.ctx.get("nodes"),
            self_name=lambda: str(self.ctx.get("node_name") or ""),
        )
        #: heartbeat/node-status 同步监听器的投递槽（调度循环异步消费）
        self._node_events: deque[str] = deque(maxlen=256)
        self._dispatch_task: asyncio.Task[None] | None = None
        self._sweep_task: asyncio.Task[None] | None = None
        self._trigger_task: asyncio.Task[None] | None = None
        self._api_fiber: Fiber | None = None
        self._ui_fiber: Fiber | None = None
        self._results_duplicate_total: int = 0
        self._late_results_total: int = 0
        #: cron 表达式内存缓存：plan_id → (updated_ts, 解析结果|None, 错误|""）；
        #: updated_ts 变即失效（编辑器 v2 设计 §5.1），解析错误只告警一次（防刷屏）
        self._cron_cache: dict[str, tuple[float, CronExpr | None, str]] = {}
        #: cron 触发游标：plan_id → 最近已派发分钟槽（epoch 秒，只进不退）
        self._cron_cursors: dict[str, int] = {}
        #: cron 触发累计派发次数（含幂等去重命中，/healthz 可观测性）
        self._cron_dispatch_total: int = 0
        #: test.job.log 幂等游标：(job_id, attempt) → 已收最大 seq（重复/乱序旧包丢弃）
        self._log_cursors: dict[tuple[str, int], int] = {}
        self._logs_lost_total: int = 0
        self._logs_duplicate_total: int = 0
        #: /healthz 仪表缓存（扫描循环定期刷新，health_metrics 是同步接口）
        self._gauge_inflight_jobs: int = 0
        self._gauge_running_tasks: int = 0
        self._gauge_materials_total: int = 0

    # ================================================================ 生命周期
    async def init(self) -> object:
        """建表 → 重启恢复 → 收件 / 事件登记 → 启动调度与租约循环；返回清理函数。"""
        # 模型归属 __owner__ = 插件 id "test-core"，与本 fiber 身份一致（生产取
        # 合成包解码的插件 id、开发取 fiber 名），归属校验自然通过
        await self.ctx.db.sync_schema(
            models=[
                TestTask,
                TestStage,
                TestJob,
                TestJobResult,
                TestCaseResult,
                TestArtifact,
                TestTaskHistory,
                TestMaterial,
                TestMaterialCase,
                TestMaterialSelection,
                TestPlan,
            ]
        )
        await self._recover()
        self.ctx.comm.on(TOPIC_JOB_RESULT, self._on_job_result)
        self.ctx.comm.on(TOPIC_JOB_PROGRESS, self._on_job_progress)
        self.ctx.comm.on(TOPIC_JOB_LOG, self._on_job_log)
        # node-status 是 emit 事件：监听器必须同步（只投递 deque，调度循环消费）
        self.ctx.effect(
            lambda: self.ctx.on(_NODE_STATUS_EVENT, self._on_node_status),
            "test-core.node-status",
        )
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        self._dispatch_task.add_done_callback(self._log_task_exit)
        self._sweep_task = asyncio.create_task(self._sweep_loop())
        self._sweep_task.add_done_callback(self._log_task_exit)
        self._trigger_task = asyncio.create_task(self._trigger_loop())
        self._trigger_task.add_done_callback(self._log_task_exit)
        # 附属服务不 await：webapi / webui 缺席时 fiber 停在 PENDING，晚到自动挂载
        self._api_fiber = self.ctx.plugin(TestCoreApiService)
        self._ui_fiber = self.ctx.plugin(TestCoreUiService)
        await self._refresh_gauges()
        self.ctx.logger.info(
            "test-core 已启动：租约 %s 秒 / 最大尝试 %s 次 / 派发窗口 %s",
            self.config.lease_timeout,
            self.config.max_attempts,
            self.config.max_inflight_jobs,
        )
        return self._stop

    async def _stop(self) -> None:
        """卸载清理：取消调度 / 租约 / cron 触发循环并等待退出（gather 吸收自身取消）。"""
        tasks = [
            task
            for task in (self._dispatch_task, self._sweep_task, self._trigger_task)
            if task is not None
        ]
        self._dispatch_task = None
        self._sweep_task = None
        self._trigger_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _log_task_exit(self, task: asyncio.Task[None]) -> None:
        """后台任务退出的观察哨：意外退出记 error，不再无声消失。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("test-core 后台任务意外退出：%s", error)

    async def _recover(self) -> None:
        """重启恢复：queued 回内存队列；过期租约走 lost 路径；半途任务收敛。"""
        now = self._clock()
        jobs = await self._repo(TestJob).list()
        tasks = {task.id: task for task in await self._repo(TestTask).list()}
        queued: list[tuple[TestTask, TestJob]] = []
        for job in jobs:
            task = tasks.get(job.task_id)
            if task is None:
                continue
            if job.state == "queued":
                queued.append((task, job))
            elif job.state in ("leased", "running") and self._scheduler.lease_expired(
                job, now=now, lease_timeout=self.config.lease_timeout
            ):
                await self._handle_lost(job, reason="lease timeout on restart")
        # queued 回内存队列：priority 降序、created_ts 升序（同任务内按 job id 时序）
        queued.sort(key=lambda item: (-item[0].priority, item[0].created_ts, item[1].id))
        for task, job in queued:
            self._enqueue(job, task, created_ts=task.created_ts, avoid_node=job.node_id)
        for task in tasks.values():
            fresh = await self._repo(TestTask).get(task.id)
            if fresh is None or fresh.state not in ("pending", "planning", "running", "canceling"):
                continue
            rows = await self._job_rows(fresh.id)
            in_flight = [
                job for job in rows if job.state in ("queued", "leased", "running", "lost")
            ]
            if fresh.state == "pending":
                await self._start_first_stage(fresh)
            elif not in_flight:
                if not rows:
                    await self._resume_stage(fresh)
                elif fresh.state == "canceling":
                    await self._finish_cancel(fresh)
                else:
                    await self._evaluate_barrier(fresh.id)
            elif fresh.state == "canceling":
                for job in rows:
                    if job.state == "queued":
                        await self._cancel_queued_job(job, reason="task canceling")
        self._warn("重启恢复完成")

    # ============================================================== 提交与生命周期
    async def submit(self, spec: TaskSpec, *, source: str = "api") -> str:
        """提交任务，返回 task_id；idempotency_key 命中返回既有任务。"""
        ensure_valid(spec)
        now = self._clock()
        if spec.idempotency_key:
            existing = await self._repo(TestTask).list(
                where=(TestTask.source == source)
                & (TestTask.idempotency_key == spec.idempotency_key)
            )
            if existing:
                return existing[0].id
        # task.transform：落库前执行，以变换后 spec 落库（DESIGN §11.2）
        hook_ctx = HookCtx(chain=CHAIN_TASK_TRANSFORM, spec=spec, meta={"source": source})
        hook_ctx = await self._hooks.run_task_transform(hook_ctx)
        spec = hook_ctx.spec if hook_ctx.spec is not None else spec
        ensure_valid(spec)

        labels = dict(spec.labels or {})
        params = dict(spec.params or {})
        # 材料/选择集引用防悬空（executor_pytest DESIGN §18 注）：顶层与各 Stage params 都查
        await self._check_material_refs(params, where="task params")
        for index, stage_spec in enumerate(spec.stages, start=1):
            await self._check_material_refs(
                dict(stage_spec.params or {}), where=f"stages[{index}].params"
            )
        if spec.timeout_s > 0:
            params[TASK_TIMEOUT_KEY] = float(spec.timeout_s)  # 模型缺口：暂存进 params
        task_id = _new_id("t", now)
        await self._repo(TestTask).add(
            TestTask(
                id=task_id,
                idempotency_key=spec.idempotency_key,
                source=source,
                name=spec.name,
                created_by=source,
                priority=int(spec.priority),
                labels=_json_dumps(labels),
                params=_json_dumps(params),
                state="pending",
                current_stage="",
                failure_policy=spec.failure_policy,
                error="",
                created_ts=now,
                updated_ts=now,
                finished_ts=0.0,
            )
        )
        stages = list(spec.stages)
        for seq, stage_spec in enumerate(stages, start=1):
            await self._repo(TestStage).add(
                TestStage(
                    id=_new_id("s", now),
                    task_id=task_id,
                    seq=seq,
                    name=stage_spec.name,
                    kind="run",
                    executor=_json_dumps(
                        {
                            "id": stage_spec.executor.id,
                            "kind": stage_spec.executor.kind,
                            "labels": dict(stage_spec.executor.labels or {}),
                            "nodes": list(stage_spec.executor.nodes or ()),
                            "strategy": stage_spec.executor.strategy,
                        }
                    ),
                    params=_json_dumps(dict(stage_spec.params or {})),
                    shard_mode=stage_spec.shard.mode,
                    shard_hint=int(stage_spec.shard.hint),
                    requires=_json_dumps(dict(stage_spec.requires or {})),
                    state="pending",
                    timeout_s=float(stage_spec.timeout_s),
                )
            )
        await self._emit_task_created(
            task_id=task_id,
            name=spec.name,
            labels=labels,
            source=source,
            priority=int(spec.priority),
        )
        # pending → planning / running 并激活首个 Stage
        task = await self._repo(TestTask).get(task_id)
        assert task is not None
        await self._start_first_stage(task)
        return task_id

    async def cancel(self, task_id: str, reason: str = "") -> bool:
        """请求取消任务；已终结或不存在返回 False。"""
        task = await self._repo(TestTask).get(task_id)
        if task is None or task.state in TASK_FINISHED:
            return False
        now = self._clock()
        note = reason or "cancel requested"
        if task.state != "canceling":
            await self._transition_task(task, "canceling", reason=note)
        has_inflight = False
        for job in await self._repo(TestJob).list(where=TestJob.task_id == task_id):
            if job.state == "queued":
                await self._cancel_queued_job(job, reason=note)
            elif job.state in ("leased", "running"):
                self._scheduler.send_cancel(
                    job.node_id, job_id=job.id, attempt=job.attempt, reason=note
                )
                has_inflight = True
            elif job.state == "lost":
                has_inflight = True  # 重派 / 超时路径收敛（届时按取消口径收尾）
        if not has_inflight:
            await self._finish_cancel(task, now=now)
        return True

    async def rerun_failed(self, task_id: str) -> str:
        """重建失败 Job（同一 job 行 attempt+1，failed→queued），返回同一 task_id。"""
        task = await self._repo(TestTask).get(task_id)
        if task is None:
            raise TaskNotFound(f"任务不存在：{task_id}")
        if task.state != "failed":
            raise InvalidTransition(f"只有 failed 任务可重跑，当前状态 {task.state!r}")
        failed_jobs = [
            job
            for job in await self._repo(TestJob).list(where=TestJob.task_id == task_id)
            if job.state == "failed"
        ]
        if not failed_jobs:
            return task_id  # 无失败 Job：原样返回（no-op）
        now = self._clock()
        await self._transition_task(task, "running", reason="rerun_failed")
        for job in failed_jobs:
            await self._update_job(
                job,
                "queued",
                reason="rerun_failed",
                attempt=job.attempt + 1,
                node_id="",
                lease_ts=0.0,
                finished_ts=0.0,
                error="",
            )
            self._enqueue(job, task, created_ts=now)
        return task_id

    async def set_priority(self, task_id: str, priority: int) -> bool:
        """调整任务优先级；不存在或已终结返回 False。"""
        task = await self._repo(TestTask).get(task_id)
        if task is None or task.state in TASK_FINISHED:
            return False
        await self._repo(TestTask).update(
            task.id, {"priority": int(priority), "updated_ts": self._clock()}
        )
        self._scheduler.reprioritize(task_id, int(priority))
        return True

    # ============================================================== 材料服务面
    # （executor_pytest DESIGN §3/§4：材料 / 用例清单 / 选择集归核心存储，内容
    # 不透明；recipe 为执行器自定义不透明字符串——core 只存取、不解析、不校验；
    # 去重键 (sha256, recipe_fingerprint)：同内容同字符串 = 同一材料（幂等复用），
    # 同内容不同字符串 = 不同测试工程；状态机 §3.1，引用约束 §3.2）

    def _materials_dir(self) -> Path:
        """材料存储目录：config.materials_dir 空取 data/plugins/test_core/materials。"""
        configured = self.config.materials_dir.strip()
        if configured:
            path = Path(configured)
            return path if path.is_absolute() else _base_dir(self.ctx) / path
        return _base_dir(self.ctx) / "data" / "plugins" / "test_core" / "materials"

    def _bundle_path(self, material_id: str) -> Path:
        """材料 zip 本体路径：materials_dir/{material_id}/bundle.zip（§3）。"""
        return self._materials_dir() / material_id / "bundle.zip"

    def _material_dict(self, row: TestMaterial) -> dict[str, Any]:
        """材料行 → 对外 dict（recipe 原样字符串、problems/labels 解码，uri 指向 zip 本体）。"""
        return {
            "id": row.id,
            "material_id": row.id,
            "name": row.name,
            "executor_id": row.executor_id,
            "kind": row.kind,
            "size": row.size,
            "sha256": row.sha256,
            "recipe_fingerprint": row.recipe_fingerprint,
            "recipe": row.recipe,
            "state": row.state,
            "problems": _decode_list(row.problems),
            "source": row.source,
            "created_by": row.created_by,
            "labels": _decode_dict(row.labels),
            "created_ts": row.created_ts,
            "updated_ts": row.updated_ts,
            "uri": f"local:///{self._bundle_path(row.id).as_posix()}",
        }

    async def _emit_material_state(
        self, *, material_id: str, from_state: str, to: str, reason: str
    ) -> None:
        """材料状态迁移广播（test/material-state）+ logger 审计（task_history 可省）。"""
        self.ctx.logger.info(
            "材料 %s 状态：%s → %s（%s）", material_id, from_state or "[*]", to, reason
        )
        try:
            await self.ctx.parallel(
                EVENT_MATERIAL_STATE,
                {
                    "schema_version": SCHEMA_VERSION,
                    "material_id": material_id,
                    "from": from_state,
                    "to": to,
                    "reason": reason,
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error(EVENT_MATERIAL_STATE, exc)

    async def upload_material(
        self,
        *,
        name: str,
        data: bytes,
        recipe: str = "",
        source: str = "api",
        created_by: str = "",
        labels: Mapping[str, str] | None = None,
        executor_id: str = "",
    ) -> Mapping[str, Any]:
        """落盘+入库；(sha256, recipe_fingerprint) 命中返回既有材料 dict（幂等）。

        ``recipe`` 为执行器自定义不透明字符串（core 不解析、不校验，原样存储）；
        去重语义：同内容同字符串 = 同一材料，同内容不同字符串 = 不同测试工程。
        返回 dict 含 id/state/…，另带 ``deduplicated`` 标记是否命中去重（§4.3）。
        ``executor_id`` 为材料归属执行器（空 = 通用材料，§4.5）。
        """
        now = self._clock()
        sha256 = hashlib.sha256(data).hexdigest()
        fingerprint = _recipe_fingerprint(recipe)
        repo = self._repo(TestMaterial)
        existing = await repo.list(
            where=(TestMaterial.sha256 == sha256)
            & (TestMaterial.recipe_fingerprint == fingerprint)
        )
        if existing:
            self.ctx.logger.info(
                "材料上传去重命中（sha256=%s…，指纹 %s）：%s",
                sha256[:12],
                fingerprint,
                existing[0].id,
            )
            dedup = self._material_dict(existing[0])
            dedup["deduplicated"] = True
            return dedup
        material_id = _new_id("m", now)
        path = self._bundle_path(material_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        row = TestMaterial(
            id=material_id,
            name=name,
            executor_id=executor_id,
            kind="zip",
            size=len(data),
            sha256=sha256,
            recipe_fingerprint=fingerprint,
            recipe=recipe,
            state="uploaded",
            problems=_json_dumps([]),
            source=source,
            created_by=created_by,
            labels=_json_dumps(dict(labels or {})),
            created_ts=now,
            updated_ts=now,
        )
        await repo.add(row)
        await self._emit_material_state(
            material_id=material_id, from_state="", to="uploaded", reason="uploaded"
        )
        out = self._material_dict(row)
        out["deduplicated"] = False
        return out

    async def list_materials(
        self,
        *,
        state: str = "",
        labels: Mapping[str, str] | None = None,
        limit: int = 50,
        offset: int = 0,
        executor_id: str = "",
    ) -> list[Mapping[str, Any]]:
        """材料列表（state 精确过滤、labels 子集匹配、分页）。

        ``executor_id`` 过滤谓词 ``executor_id == X OR executor_id == ""``
        （通用材料恒可见）；空串 = 不按归属过滤（§4.5）。
        """
        where = TestMaterial.state == state if state else None
        rows = await self._repo(TestMaterial).list(
            where=where, order_by=TestMaterial.created_ts
        )
        if executor_id:
            rows = [row for row in rows if row.executor_id in ("", executor_id)]
        wanted = dict(labels or {})
        if wanted:
            rows = [
                row
                for row in rows
                if all(_decode_dict(row.labels).get(k) == v for k, v in wanted.items())
            ]
        start = max(0, offset)
        end = start + max(0, limit)
        return [self._material_dict(row) for row in rows[start:end]]

    async def get_material(self, material_id: str) -> Mapping[str, Any] | None:
        """材料详情；不存在返回 None。"""
        row = await self._repo(TestMaterial).get(material_id)
        return self._material_dict(row) if row is not None else None

    async def get_material_bytes(self, material_id: str) -> bytes | None:
        """材料 zip 本体；不存在（行或文件）返回 None。"""
        row = await self._repo(TestMaterial).get(material_id)
        if row is None:
            return None
        path = self._bundle_path(row.id)
        return path.read_bytes() if path.is_file() else None

    async def update_recipe(
        self, material_id: str, recipe: str
    ) -> Mapping[str, Any]:
        """更新配方（执行器自定义不透明字符串，原样存储、指纹重算、状态回
        uploaded 需重分析，§3.1）；不存在抛 MaterialNotFound。"""
        repo = self._repo(TestMaterial)
        row = await repo.get(material_id)
        if row is None:
            raise MaterialNotFound(f"材料不存在：{material_id}")
        now = self._clock()
        from_state = row.state
        row.recipe = recipe
        row.recipe_fingerprint = _recipe_fingerprint(recipe)
        row.state = "uploaded"
        row.updated_ts = now
        await repo.update(
            material_id,
            {
                "recipe": row.recipe,
                "recipe_fingerprint": row.recipe_fingerprint,
                "state": row.state,
                "updated_ts": now,
            },
        )
        if from_state != row.state:
            await self._emit_material_state(
                material_id=material_id,
                from_state=from_state,
                to=row.state,
                reason="recipe updated",
            )
        return self._material_dict(row)

    async def put_problems(
        self, material_id: str, problems: Sequence[Mapping[str, Any]], *, state: str
    ) -> bool:
        """inspect 结果写入（problems 全量替换，§3.1）；state ∈ {"analyzed","failed"}；不存在返回 False。"""
        if state not in ("analyzed", "failed"):
            raise ValueError(f"state 只能是 analyzed / failed，收到 {state!r}")
        repo = self._repo(TestMaterial)
        row = await repo.get(material_id)
        if row is None:
            return False
        now = self._clock()
        from_state = row.state
        row.problems = _json_dumps([dict(item) for item in problems])
        row.state = state
        row.updated_ts = now
        await repo.update(
            material_id, {"problems": row.problems, "state": state, "updated_ts": now}
        )
        if from_state != state:
            await self._emit_material_state(
                material_id=material_id, from_state=from_state, to=state, reason="inspect result"
            )
        return True

    async def put_cases(
        self, material_id: str, cases: Sequence[Mapping[str, Any]]
    ) -> int:
        """全量替换该材料用例清单（幂等：id = material_id::case_id，§3），返回条数。

        不存在抛 :class:`MaterialNotFound`；输入内重复 case_id 保留首条。
        """
        if await self._repo(TestMaterial).get(material_id) is None:
            raise MaterialNotFound(f"材料不存在：{material_id}")
        repo = self._repo(TestMaterialCase)
        for row in await repo.list(where=TestMaterialCase.material_id == material_id):
            await repo.delete(row.id)
        count = 0
        seen: set[str] = set()
        for item in cases:
            case_id = str(item.get("case_id") or "")
            if not case_id or case_id in seen:
                continue
            seen.add(case_id)
            markers = item.get("markers")
            params = item.get("params")
            await repo.add(
                TestMaterialCase(
                    id=f"{material_id}::{case_id}",
                    material_id=material_id,
                    case_id=case_id,
                    name=str(item.get("name") or ""),
                    path=str(item.get("path") or ""),
                    markers=_json_dumps(
                        list(markers) if isinstance(markers, (list, tuple)) else []
                    ),
                    params=_json_dumps(dict(params) if isinstance(params, Mapping) else {}),
                    status=str(item.get("status") or "ok"),
                )
            )
            count += 1
        return count

    async def list_material_cases(self, material_id: str) -> list[Mapping[str, Any]]:
        """材料的用例清单（material_case 行，按 id 排序）。"""
        rows = await self._repo(TestMaterialCase).list(
            where=TestMaterialCase.material_id == material_id,
            order_by=TestMaterialCase.id,
        )
        return [
            {
                "id": row.id,
                "material_id": row.material_id,
                "case_id": row.case_id,
                "name": row.name,
                "path": row.path,
                "markers": _decode_list(row.markers),
                "params": _decode_dict(row.params),
                "status": row.status,
            }
            for row in rows
        ]

    async def save_selection(
        self, material_id: str, name: str, case_ids: Sequence[str], *, created_by: str = ""
    ) -> str:
        """保存选择集（Job 只带 selection_id 引用，D19），返回 selection_id。"""
        if await self._repo(TestMaterial).get(material_id) is None:
            raise MaterialNotFound(f"材料不存在：{material_id}")
        now = self._clock()
        selection_id = _new_id("sel", now)
        await self._repo(TestMaterialSelection).add(
            TestMaterialSelection(
                id=selection_id,
                material_id=material_id,
                name=name,
                case_ids=_json_dumps([str(case_id) for case_id in case_ids]),
                created_by=created_by,
                created_ts=now,
            )
        )
        return selection_id

    async def get_selection(self, selection_id: str) -> Mapping[str, Any] | None:
        """选择集详情；不存在返回 None。"""
        row = await self._repo(TestMaterialSelection).get(selection_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "selection_id": row.id,
            "material_id": row.material_id,
            "name": row.name,
            "case_ids": _decode_list(row.case_ids),
            "created_by": row.created_by,
            "created_ts": row.created_ts,
        }

    async def delete_material(self, material_id: str) -> bool:
        """删除材料（行 + 用例清单 + zip 目录）；有引用抛 MaterialInUse，不存在抛 MaterialNotFound（§3.2）。"""
        repo = self._repo(TestMaterial)
        if await repo.get(material_id) is None:
            raise MaterialNotFound(f"材料不存在：{material_id}")
        if await self._material_referenced(material_id):
            raise MaterialInUse(f"材料被引用（任务 params / 计划步骤 / 选择集在用），禁止删除：{material_id}")
        await repo.delete(material_id)
        case_repo = self._repo(TestMaterialCase)
        for row in await case_repo.list(
            where=TestMaterialCase.material_id == material_id
        ):
            await case_repo.delete(row.id)
        shutil.rmtree(self._bundle_path(material_id).parent, ignore_errors=True)
        self.ctx.logger.info("材料已删除：%s", material_id)
        return True

    async def list_unreferenced(self, *, older_than_ts: float) -> list[Mapping[str, Any]]:
        """GC 辅助：无引用且 updated_ts < older_than_ts 的材料（§3.2）。"""
        rows = await self._repo(TestMaterial).list(
            where=TestMaterial.updated_ts < older_than_ts,
            order_by=TestMaterial.updated_ts,
        )
        out: list[Mapping[str, Any]] = []
        for row in rows:
            if not await self._material_referenced(row.id):
                out.append(self._material_dict(row))
        return out

    async def _material_referenced(self, material_id: str) -> bool:
        """引用检查（§3.2 / 评审 #12）：任务（含 Stage）params、计划 steps 的
        JSON 文本含该 id，或选择集关联。"""
        pattern = f"%{material_id}%"
        if await self._repo(TestTask).count(where=TestTask.params.like(pattern)) > 0:
            return True
        if await self._repo(TestStage).count(where=TestStage.params.like(pattern)) > 0:
            return True
        if await self._repo(TestPlan).count(where=TestPlan.steps.like(pattern)) > 0:
            return True
        return (
            await self._repo(TestMaterialSelection).count(
                where=TestMaterialSelection.material_id == material_id
            )
            > 0
        )

    async def _check_material_refs(self, params: Mapping[str, Any], *, where: str) -> None:
        """params 带 material_id / selection_id 时防悬空（不存在抛 InvalidSpec）。"""
        material_id = params.get("material_id")
        if isinstance(material_id, str) and material_id:
            if await self._repo(TestMaterial).get(material_id) is None:
                raise InvalidSpec(f"{where} 引用的 material_id 不存在：{material_id}")
        elif material_id is not None and material_id != "":
            raise InvalidSpec(f"{where}.material_id 需是字符串，收到 {material_id!r}")
        selection_id = params.get("selection_id")
        if isinstance(selection_id, str) and selection_id:
            if await self._repo(TestMaterialSelection).get(selection_id) is None:
                raise InvalidSpec(f"{where} 引用的 selection_id 不存在：{selection_id}")
        elif selection_id is not None and selection_id != "":
            raise InvalidSpec(f"{where}.selection_id 需是字符串，收到 {selection_id!r}")

    # ============================================================== 计划服务面
    # （测试计划设计 §4：TestPlan CRUD + 下发实例化；校验链保存 / 下发共用，
    # 悬空 / 归属 / selection 交叉的异步查询内联在 _check_plan_materials）

    def _plan_dict(self, row: TestPlan) -> dict[str, Any]:
        """计划行 → 对外 dict（labels / defaults / steps 解码，trigger / notify
        归一回显；缺省 manual / none，§5）。steps 原样返回（旧格式读取归一在
        校验链 / 下发注入内做，编辑器载入时自行归一，§2.1）。

        ``next_run_ts`` 为纯增量键（§5.1 可观测性）：cron 型给下一命中分钟槽
        的 epoch 秒（本地时区，非法表达式 / 无命中为 None），其余类型恒 None。
        """
        trigger = _trigger_dict(_decode_dict(row.trigger_cfg), "manual")
        return {
            "id": row.id,
            "plan_id": row.id,
            "name": row.name,
            "description": row.description,
            "labels": _decode_dict(row.labels),
            "defaults": _decode_dict(row.defaults),
            "steps": _decode_list(row.steps),
            "trigger": trigger,
            "notify": _trigger_dict(_decode_dict(row.notify_cfg), "none"),
            "next_run_ts": self._cron_next_run(trigger),
            "created_by": row.created_by,
            "created_ts": row.created_ts,
            "updated_ts": row.updated_ts,
        }

    def _cron_next_run(self, trigger: Mapping[str, Any]) -> float | None:
        """cron 型计划下一命中槽 epoch 秒（非 cron / 非法表达式 / 无命中返回 None）。"""
        if str(trigger.get("type") or "manual") != "cron":
            return None
        raw = _as_mapping(trigger.get("config")).get("expr")
        if not isinstance(raw, str):
            return None
        try:
            expr = parse_cron(raw)
        except ValueError:
            return None
        moment = datetime.fromtimestamp(self._clock())
        nxt = expr.next_after(moment)
        return nxt.timestamp() if nxt is not None else None

    async def _check_plan_materials(self, steps: Sequence[Mapping[str, Any]]) -> list[str]:
        """计划步骤材料引用校验（编辑器 v2 设计 §3/§8）：悬空 / 版本漂移 /
        用例消失 + selection 交叉一致 + 材料归属匹配。

        异步查询内联，问题逐条收集，调用方统一抛 :class:`InvalidSpec`：

        * material_id 不存在 → 「脚本版本已删除，请重新选择脚本」（§3）；
        * ``material.sha256 / recipe_fingerprint`` 与落盘不符 → 版本漂移
          「脚本已变更（指纹不符），请重新确认用例」（无指纹的旧绑定只做
          存在性校验）；
        * ``cases.mode=selected`` 且材料无用例清单 → 「脚本尚无用例清单」；
          有清单而 case_id 缺失 → 逐条「用例已不存在：<case_id>」；
        * ``params.selection_id`` 与显式 ``cases`` 选例并存且清单不等 →
          同名语义冲突（沿 material_id 冲突先例）。
        """
        problems: list[str] = []
        for index, step in enumerate(steps):
            where = f"steps[{index}]"
            params = _plan_step_params(step)
            material = _plan_material_of(step)
            material_id_raw: Any = material.get("material_id")
            material_id = material_id_raw if isinstance(material_id_raw, str) else ""
            inner_material: Any = params.get("material_id")
            if inner_material is not None and inner_material != "" and not isinstance(inner_material, str):
                problems.append(f"{where}.params.material_id 需是字符串，收到 {inner_material!r}")
            if material_id:
                material_row = await self._repo(TestMaterial).get(material_id)
                if material_row is None:
                    problems.append(
                        f"{where} 脚本版本已删除，请重新选择脚本（material_id={material_id}）"
                    )
                else:
                    executor_id = str(_as_mapping(step.get("executor")).get("id") or "")
                    if material_row.executor_id not in ("", executor_id):
                        problems.append(
                            f"{where} 引用的材料归属不符：材料 executor_id={material_row.executor_id!r}，"
                            f"步骤 executor.id={executor_id!r}"
                        )
                    drift: list[str] = []
                    sha256 = str(material.get("sha256") or "")
                    if sha256 and sha256 != material_row.sha256:
                        drift.append("sha256")
                    recipe_fingerprint = str(material.get("recipe_fingerprint") or "")
                    if recipe_fingerprint and recipe_fingerprint != material_row.recipe_fingerprint:
                        drift.append("recipe_fingerprint")
                    if drift:
                        problems.append(
                            f"{where} 脚本已变更（指纹不符），请重新确认用例"
                            f"（{'、'.join(drift)}，material_id={material_id}）"
                        )
                    cases = _plan_cases_of(step)
                    if cases.get("mode") == "selected":
                        case_rows = await self._repo(TestMaterialCase).list(
                            where=TestMaterialCase.material_id == material_id
                        )
                        if not case_rows:
                            problems.append(f"{where} 脚本尚无用例清单（material_id={material_id}）")
                        else:
                            existing = {row.case_id for row in case_rows}
                            case_ids = cases.get("case_ids")
                            if isinstance(case_ids, list):
                                for case_id in case_ids:
                                    if isinstance(case_id, str) and case_id and case_id not in existing:
                                        problems.append(f"{where} 用例已不存在：{case_id}")
            selection_id: Any = params.get("selection_id")
            if selection_id is not None and selection_id != "" and not isinstance(selection_id, str):
                problems.append(f"{where}.params.selection_id 需是字符串，收到 {selection_id!r}")
            elif isinstance(selection_id, str) and selection_id:
                selection = await self._repo(TestMaterialSelection).get(selection_id)
                if selection is None:
                    problems.append(f"{where} 引用的 selection_id 不存在：{selection_id}")
                else:
                    if material_id and selection.material_id != material_id:
                        problems.append(
                            f"{where} 的 selection 归属不符：selection.material_id={selection.material_id!r}，"
                            f"步骤 material_id={material_id!r}"
                        )
                    cases_raw: Any = step.get("cases")
                    cases = _plan_cases_of(step)
                    wanted = cases.get("case_ids")
                    if cases_raw is not None and cases.get("mode") == "selected":
                        picked = _decode_list(selection.case_ids)
                        if isinstance(wanted, list) and not _same_str_list(wanted, picked):
                            problems.append(
                                f"{where}.cases.case_ids 与 params.selection_id 同名冲突："
                                f"{wanted!r} ≠ {picked!r}"
                            )
        return problems

    async def save_plan(
        self,
        *,
        plan_id: str = "",
        name: str,
        description: str = "",
        labels: Mapping[str, str] | None = None,
        defaults: Mapping[str, Any] | None = None,
        steps: Sequence[Mapping[str, Any]] | None = None,
        trigger: Mapping[str, Any] | None = None,
        notify: Mapping[str, Any] | None = None,
        created_by: str = "",
    ) -> Mapping[str, Any]:
        """新建或更新计划（§4.4）：plan_id 空 = 新建；更新不存在抛 PlanNotFound。

        校验链与 :meth:`dispatch_plan` 共用（编辑器 v2 设计 §8），失败抛
        InvalidSpec（逐条）；``trigger`` / ``notify`` 为预留接口（§5），
        ``{type, config}`` 归一落 JSON 文本列，缺省 manual / none；cron 型
        另校验 ``config.expr`` 可解析（§5.1，「trigger.config.expr 非法」）。
        """
        repo = self._repo(TestPlan)
        row = await repo.get(plan_id) if plan_id else None
        if plan_id and row is None:
            raise PlanNotFound(f"计划不存在：{plan_id}")
        step_list = [dict(step) for step in (steps or ())]
        problems: list[str] = []
        if not name.strip():
            problems.append(f"name 需是非空字符串，收到 {name!r}")
        problems += _check_plan_steps(step_list)
        problems += await self._check_plan_materials(step_list)
        problems += _check_trigger_notify(trigger, notify)
        problems += _check_cron_expr(trigger)
        if problems:
            raise InvalidSpec("计划规格无效：" + "；".join(problems))
        now = self._clock()
        fields = {
            "name": name,
            "description": description,
            "labels": _json_dumps(dict(labels or {})),
            "defaults": _json_dumps(dict(defaults or {})),
            "steps": _json_dumps(step_list),
            "trigger_cfg": _json_dumps(_trigger_dict(trigger, "manual")),
            "notify_cfg": _json_dumps(_trigger_dict(notify, "none")),
        }
        if row is not None:
            await repo.update(plan_id, {**fields, "updated_ts": now})
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_ts = now
            return self._plan_dict(row)
        row = TestPlan(
            id=_new_id("p", now),
            created_by=created_by,
            created_ts=now,
            updated_ts=now,
            **fields,
        )
        await repo.add(row)
        return self._plan_dict(row)

    async def list_plans(
        self,
        *,
        keyword: str = "",
        labels: Mapping[str, str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        """计划列表（keyword 模糊匹配 name/description、labels 子集匹配、分页）。"""
        rows: list[TestPlan] = await self._repo(TestPlan).list(
            order_by=TestPlan.created_ts
        )
        key = keyword.strip().lower()
        if key:
            rows = [
                row
                for row in rows
                if key in row.name.lower() or key in row.description.lower()
            ]
        wanted = dict(labels or {})
        if wanted:
            rows = [
                row
                for row in rows
                if all(_decode_dict(row.labels).get(k) == v for k, v in wanted.items())
            ]
        start = max(0, offset)
        end = start + max(0, limit)
        return [self._plan_dict(row) for row in rows[start:end]]

    async def get_plan(self, plan_id: str) -> Mapping[str, Any] | None:
        """计划详情；不存在返回 None。"""
        row = await self._repo(TestPlan).get(plan_id)
        return self._plan_dict(row) if row is not None else None

    async def delete_plan(self, plan_id: str) -> bool:
        """删除计划；不存在返回 False（已删计划的任务仍经 labels 回显，评审 #21）。"""
        repo = self._repo(TestPlan)
        if await repo.get(plan_id) is None:
            return False
        await repo.delete(plan_id)
        return True

    async def dispatch_plan(
        self, plan_id: str, overrides: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        """下发计划（§4.3/§5.5）：实例化 TaskSpec 提交，返回 {task_id, deduplicated}。

        合并次序（评审 #8）：每步先 ``stage_params[步骤名]`` 浅合并（同名替换、
        数组整体替换）→ 再 ``env`` 增量注入每步 ``params.env``（后者胜）；
        ``AETP_*`` 环境键拒绝注入（评审 #9）；幂等域 ``source="plan-dispatch"``
        （评审 #13，缺省空 key = 每次新建）。
        """
        plan = await self._repo(TestPlan).get(plan_id)
        if plan is None:
            raise PlanNotFound(f"计划不存在：{plan_id}")
        ov = dict(overrides or {})
        problems: list[str] = [
            f"overrides 未知键：{key!r}" for key in sorted(ov) if key not in _PLAN_OVERRIDE_KEYS
        ]
        name_override: Any = ov.get("name", "")
        if name_override is not None and not isinstance(name_override, str):
            problems.append(f"overrides.name 需是字符串，收到 {name_override!r}")
            name_override = ""
        labels_raw: Any = ov.get("labels")
        if labels_raw is not None and not _is_mapping(labels_raw):
            problems.append(f"overrides.labels 需是对象，收到 {labels_raw!r}")
        labels_override = _as_mapping(labels_raw)
        env_raw: Any = ov.get("env")
        if env_raw is not None and not _is_mapping(env_raw):
            problems.append(f"overrides.env 需是对象，收到 {env_raw!r}")
        env_override = _as_mapping(env_raw)
        stage_params_raw: Any = ov.get("stage_params")
        if stage_params_raw is not None and not _is_mapping(stage_params_raw):
            problems.append(f"overrides.stage_params 需是对象，收到 {stage_params_raw!r}")
        stage_params = _as_mapping(stage_params_raw)
        idem_override: Any = ov.get("idempotency_key")
        if idem_override is not None and not isinstance(idem_override, str):
            problems.append(f"overrides.idempotency_key 需是字符串，收到 {idem_override!r}")
            idem_override = ""

        # AETP_* 环境键（overrides.env 与 stage_params[*].env，评审 #9）
        for key in env_override:
            if str(key).startswith(_ENV_RESERVED_PREFIX):
                problems.append(f"overrides.env 含保留键：{key!r}（{_ENV_RESERVED_PREFIX}* 禁止注入）")
        for step_key, override_params in stage_params.items():
            if override_params is not None and not _is_mapping(override_params):
                problems.append(f"stage_params[{step_key!r}] 需是对象，收到 {override_params!r}")
                continue
            step_env: Any = _as_mapping(override_params).get("env")
            if _is_mapping(step_env):
                for key in _as_mapping(step_env):
                    if str(key).startswith(_ENV_RESERVED_PREFIX):
                        problems.append(
                            f"stage_params[{step_key!r}].env 含保留键：{key!r}"
                            f"（{_ENV_RESERVED_PREFIX}* 禁止注入）"
                        )

        raw_steps = [
            _as_mapping(item) for item in _decode_list(plan.steps) if _is_mapping(item)
        ]
        # stage_params 键必须命中步骤名（评审 #7）
        step_names = {str(step.get("name") or "") for step in raw_steps}
        for key in stage_params:
            if key not in step_names:
                problems.append(f"stage_params 键未命中步骤名：{key!r}")

        # 合并次序（评审 #8）：stage_params 浅合并（同名替换、数组整体替换）
        # → env 增量注入每步 params.env（后者胜）
        merged_steps: list[dict[str, Any]] = []
        for step in raw_steps:
            merged = dict(step)
            params = _plan_step_params(step)
            override_params = stage_params.get(str(step.get("name") or ""))
            if _is_mapping(override_params):
                params = {**params, **_as_mapping(override_params)}
            if env_override:
                params["env"] = {**_as_mapping(params.get("env")), **env_override}
            merged["params"] = params
            merged_steps.append(merged)

        # 校验链（与保存共用，编辑器 v2 设计 §8）：steps 非空 / name 唯一 /
        # executor.id / material 绑定与指纹 / cases / 同名冲突 / 悬空 / selection
        # 交叉 / 材料归属；计划级 trigger/notify 枚举复验（老库坏行兜底，§8.7）
        problems += _check_plan_steps(merged_steps)
        problems += await self._check_plan_materials(merged_steps)
        problems += _check_trigger_notify(
            _decode_dict(plan.trigger_cfg), _decode_dict(plan.notify_cfg)
        )
        if problems:
            raise InvalidSpec("计划规格无效：" + "；".join(problems))

        # 组装 TaskSpec（§4.3）：steps→stages、defaults→顶层、labels 注入
        defaults = _decode_dict(plan.defaults)
        name = str(name_override or plan.name)
        priority_value: Any = ov.get("priority") or defaults.get("priority") or 0
        timeout_value: Any = defaults.get("timeout_s") or 0
        spec = TaskSpec(
            name=name,
            stages=_plan_stage_specs(merged_steps),
            priority=priority_value,
            labels={
                **_decode_dict(plan.labels),
                **labels_override,
                LABEL_PLAN_ID: plan_id,
                LABEL_PLAN_NAME: plan.name,
            },
            idempotency_key=str(idem_override or ""),
            failure_policy=cast(
                "Literal['fail_fast', 'continue']",
                str(defaults.get("failure_policy") or "fail_fast"),
            ),
            timeout_s=timeout_value,
        )
        # 幂等判定（评审 #13）：(source="plan-dispatch", idempotency_key) 预查
        deduplicated = False
        if spec.idempotency_key:
            existing = await self._repo(TestTask).list(
                where=(TestTask.source == "plan-dispatch")
                & (TestTask.idempotency_key == spec.idempotency_key)
            )
            deduplicated = bool(existing)
        task_id = await self.submit(spec, source="plan-dispatch")
        return {"task_id": task_id, "deduplicated": deduplicated}

    # ============================================================== 状态机编排
    def _repo(self, model: Any) -> Any:
        """按模型归属取仓储。

        显式 ``owner=``（= 模型物理归属，db 的显式授权口）：服务方法经**调用方**
        fiber 执行（如 api 附属服务），调用方身份与模型归属可能不同名；显式归属
        仍受「owner 必须等于模型物理归属」校验。
        """
        return self.ctx.db.repo(model, owner=MODEL_OWNER)

    def _enqueue(
        self,
        job: TestJob,
        task: TestTask,
        *,
        created_ts: float,
        avoid_node: str = "",
    ) -> None:
        """job 入内存就绪队列（project 配额轴取任务 labels）。"""
        self._scheduler.enqueue(
            job.id,
            task.id,
            priority=task.priority,
            created_ts=created_ts,
            project=project_of(_decode_dict(task.labels)),
            avoid_node=avoid_node,
        )

    async def _transition_task(
        self, task: TestTask, to: str, *, reason: str, error: str | None = None
    ) -> None:
        """任务状态迁移：写行 + task_history + emit test/task-state（§5.1）。"""
        now = self._clock()
        from_state = task.state
        transition("task", from_state, to, reason=reason)
        values: dict[str, object] = {"state": to, "updated_ts": now}
        if to in TASK_FINISHED:
            values["finished_ts"] = now
            values["error"] = error if error is not None else (reason if to in ("failed", "error") else "")
        else:
            values["finished_ts"] = 0.0
        await self._repo(TestTask).update(task.id, values)
        task.state = to
        await self._append_history(task.id, from_state, to, reason, now)
        await self._emit_task_state(
            task_id=task.id, from_state=from_state, to=to, reason=reason, at=now
        )

    async def _append_history(
        self, task_id: str, from_state: str, to_state: str, reason: str, now: float
    ) -> None:
        """写 task_history 并按 history_size 裁剪（DESIGN §13）。"""
        repo = self._repo(TestTaskHistory)
        await repo.add(
            TestTaskHistory(
                **history_row(task_id, from_state, to_state, reason, now, _new_id("h", now))
            )
        )
        rows = await repo.list(
            where=TestTaskHistory.task_id == task_id,
            order_by=[TestTaskHistory.ts, TestTaskHistory.id],
        )
        excess = len(rows) - self.config.history_size
        for row in rows[: max(0, excess)]:
            await repo.delete(row.id)

    async def _update_job(
        self, job: TestJob, to: str, *, reason: str = "", **values: object
    ) -> None:
        """job 状态迁移（registry.transition 校验）+ 写行。"""
        transition("job", job.state, to, reason=reason)
        data: dict[str, object] = {"state": to, **values}
        await self._repo(TestJob).update(job.id, data)
        job.state = to

    async def _start_first_stage(self, task: TestTask) -> None:
        """激活首个 Stage：pending → planning / running（plan 型先置 planning）。"""
        stages = await self._stage_rows(task.id)
        if not stages:
            await self._transition_task(
                task, "error", reason="task has no stages", error="task has no stages"
            )
            return
        first = stages[0]
        to = "planning" if first.shard_mode == "plan" else "running"
        await self._transition_task(task, to, reason="submit")
        await self._start_stage(task, first)

    async def _resume_stage(self, task: TestTask) -> None:
        """恢复半途任务：当前 Stage 无任何 job 时补建（崩溃在建 job 前）。"""
        stages = await self._stage_rows(task.id)
        current = next((s for s in stages if s.name == task.current_stage), None)
        if current is None:
            current = stages[0] if stages else None
        if current is None:
            await self._transition_task(
                task, "error", reason="task has no stages", error="task has no stages"
            )
            return
        await self._start_stage(task, current)

    async def _start_stage(self, task: TestTask, stage: TestStage) -> None:
        """激活 Stage：建 plan / run job 入队（§5.1 / §9.3）。"""
        now = self._clock()
        await self._repo(TestStage).update(stage.id, {"state": "running"})
        await self._repo(TestTask).update(
            task.id, {"current_stage": stage.name, "updated_ts": now}
        )
        task.current_stage = stage.name
        if stage.shard_mode == "plan":
            # plan Job 的 shard 载荷带 hint：分片规划的期望数由执行器裁量
            await self._create_job(
                task, stage, shard={"plan": True, "hint": int(stage.shard_hint or 0)}
            )
        else:
            await self._create_job(task, stage, shard=None)

    async def _create_job(
        self, task: TestTask, stage: TestStage, *, shard: dict[str, Any] | None
    ) -> TestJob:
        """建 job 行（attempt=1）并入就绪队列。"""
        now = self._clock()
        job = TestJob(
            id=_new_id("j", now),
            stage_id=stage.id,
            task_id=task.id,
            node_id="",
            attempt=1,
            state="queued",
            shard=_json_dumps(shard) if shard is not None else "null",
            lease_ts=0.0,
            started_ts=0.0,
            finished_ts=0.0,
            error="",
            result_seq=0,
        )
        await self._repo(TestJob).add(job)
        self._enqueue(job, task, created_ts=now)
        return job

    async def _stage_rows(self, task_id: str) -> list[TestStage]:
        """任务的全部 Stage（seq 升序）。"""
        return await self._repo(TestStage).list(
            where=TestStage.task_id == task_id, order_by=TestStage.seq
        )

    async def _job_rows(self, task_id: str) -> list[TestJob]:
        """任务的全部 Job 行。"""
        return await self._repo(TestJob).list(where=TestJob.task_id == task_id)

    # ============================================================== 调度循环
    async def _dispatch_loop(self) -> None:
        """派发循环：按窗口 / 配额出队派发（异常隔离，不退出循环）。"""
        while True:
            await asyncio.sleep(_DISPATCH_INTERVAL)
            try:
                await self._dispatch_tick()
            except Exception as error:  # noqa: BLE001
                self.ctx.logger.error("调度循环出错：%s", error)

    async def _dispatch_tick(self) -> None:
        """一轮派发：窗口内逐个选节点派发，直到队空 / 满窗 / 无候选节点。"""
        if self._scheduler.queue_depth() == 0:
            return
        cfg = self.config
        inflight = await self._scheduler.inflight()
        skip: set[str] = set()
        while inflight.total < cfg.max_inflight_jobs:
            entry = self._scheduler.peek(
                inflight=inflight, project_quota=cfg.project_quota, skip=skip
            )
            if entry is None:
                return
            job = await self._repo(TestJob).get(entry.job_id)
            if job is None or job.state != "queued":
                self._scheduler.drop(entry.job_id)
                continue
            task = await self._repo(TestTask).get(job.task_id)
            if task is None:
                self._scheduler.drop(entry.job_id)
                continue
            if task.state == "canceling" or task.state in TASK_FINISHED:
                await self._cancel_queued_job(job, reason="task no longer active")
                continue
            if task.state not in ("planning", "running"):
                self._scheduler.drop(entry.job_id)
                continue
            stage = await self._repo(TestStage).get(job.stage_id)
            if stage is None:
                self._scheduler.drop(entry.job_id)
                continue
            executor = _executor_of(stage)
            node_id = self._scheduler.pick_node(
                executor, inflight=inflight, avoid=entry.avoid_node
            )
            if node_id is None:
                return  # 无候选节点：保留队首，下一轮再试
            now = self._clock()
            timeout = stage.timeout_s if stage.timeout_s > 0 else cfg.job_timeout
            shard = _decode_json(job.shard, None)
            descriptor = JobDescriptor(
                proto=TEST_PROTO,
                job_id=job.id,
                task_id=task.id,
                stage_name=stage.name,
                attempt=job.attempt,
                executor=executor,
                params=_job_params(task, stage, shard),
                shard=_descriptor_shard(shard),
                artifact_dir=f"data/plugins/test_core/artifacts/{task.id}/{job.id}/a{job.attempt}",
                deadline_ts=now + timeout,
            )
            # 派发即 job：queued → leased（不发事件，§5.2）
            await self._update_job(
                job,
                "leased",
                reason="dispatch",
                node_id=node_id,
                lease_ts=now,
            )
            self._scheduler.take(job.id)
            self._scheduler.send_job(node_id, descriptor, ttl=timeout)
            inflight.note(node_id, project_of(_decode_dict(task.labels)))

    async def _cancel_queued_job(self, job: TestJob, *, reason: str) -> None:
        """取消排队中的 job（queued → canceled）：出队 + 收尾 + 屏障评估。"""
        self._scheduler.drop(job.id)
        await self._finish_job(job, "canceled", summary={}, error=reason or "canceled")

    # ============================================================== 租约扫描
    async def _sweep_loop(self) -> None:
        """租约扫描循环：周期 max(1, min(5, lease_timeout/4))（§10）。"""
        period = max(1.0, min(5.0, self.config.lease_timeout / 4.0))
        while True:
            await asyncio.sleep(period)
            try:
                await self._sweep_once()
            except Exception as error:  # noqa: BLE001
                self.ctx.logger.error("租约扫描出错：%s", error)

    async def _sweep_once(self) -> None:
        """一轮扫描：节点失联提前 lost → 租约超时 → 任务总超时 → 仪表刷新。"""
        now = self._clock()
        seen: set[str] = set()
        while self._node_events:
            node_id = self._node_events.popleft()
            if node_id in seen:
                continue
            seen.add(node_id)
            for job in await self._scheduler.jobs_on_node(node_id):
                self._scheduler.count_lease_timeout()
                await self._handle_lost(job, reason=f"node {node_id} offline/lost")
        for job in await self._scheduler.inflight_jobs():
            if self._scheduler.lease_expired(
                job, now=now, lease_timeout=self.config.lease_timeout
            ):
                self._scheduler.count_lease_timeout()
                await self._handle_lost(job, reason="lease timeout")
        await self._fail_timeout_tasks(now)
        await self._refresh_gauges()

    async def _fail_timeout_tasks(self, now: float) -> None:
        """任务总超时：running → failed（error="task timeout"），在途 job 发取消。"""
        for task in await self._repo(TestTask).list(where=TestTask.state == "running"):
            timeout = self._scheduler.task_timeout_of(task)
            if timeout <= 0 or (now - task.created_ts) <= timeout:
                continue
            # 先落终态（屏障对已终结任务幂等），再处置在途 job
            await self._transition_task(
                task, "failed", reason="task timeout", error="task timeout"
            )
            for job in await self._job_rows(task.id):
                if job.state == "queued":
                    await self._cancel_queued_job(job, reason="task timeout")
                elif job.state in ("leased", "running"):
                    self._scheduler.send_cancel(
                        job.node_id,
                        job_id=job.id,
                        attempt=job.attempt,
                        reason="task timeout",
                    )
            counts = await self._aggregate_counts(task.id)
            await self._emit_task_finished(
                task_id=task.id,
                state="failed",
                counts=counts,
                duration_s=max(0.0, now - task.created_ts),
            )

    async def _handle_lost(self, job: TestJob, *, reason: str) -> None:
        """lost 路径：未超 max_attempts → attempt+1 重新入队（换节点优先）；否则 failed。"""
        now = self._clock()
        if job.state not in ("leased", "running"):
            return
        await self._update_job(job, "lost", reason=reason, error=reason)
        task = await self._repo(TestTask).get(job.task_id)
        if task is not None and task.state in TASK_FINISHED:
            # 任务已终结（超时/取消收尾后残留）：不再重派，直接判失败收口
            await self._finish_job(job, "failed", summary={}, error="task already finished", now=now)
            return
        if job.attempt < self.config.max_attempts:
            await self._update_job(
                job,
                "queued",
                reason=reason,
                attempt=job.attempt + 1,
                node_id="",
                lease_ts=0.0,
                error=reason,
            )
            self._scheduler.count_retry()
            if task is not None:
                self._enqueue(job, task, created_ts=now, avoid_node=job.node_id)
            return
        await self._finish_job(job, "failed", summary={}, error=reason, now=now)

    # ============================================================== cron 触发
    async def _trigger_loop(self) -> None:
        """cron 触发循环（编辑器 v2 设计 §5.1）：每 tick 扫描 cron 计划并对命中的
        当前分钟槽派发；错过即跳过（不做 backlog 补派），异常隔离不退出循环。"""
        while True:
            await asyncio.sleep(_TRIGGER_INTERVAL)
            try:
                await self._trigger_tick()
            except Exception as error:  # noqa: BLE001
                self.ctx.logger.error("cron 触发循环出错：%s", error)

    async def _trigger_tick(self) -> None:
        """一轮触发扫描：当前分钟槽命中且大于游标 → dispatch_plan 并推进游标。

        * 表达式解析结果缓存于 ``_cron_cache``，按计划 ``updated_ts`` 失效；
          解析失败（老库坏行）记一次 warning 跳过（防刷屏）；
        * 幂等键 ``cron:<plan_id>:<slot>``：master 同分钟内重启重派同 slot 由
          submit 的 (source="plan-dispatch", idempotency_key) 去重兜底；
        * 游标只进不退（命中即先推进）：派发抛异常（如材料漂移校验失败）仅记
          日志不重试本槽（防刷屏），错过的历史槽不补派。
        """
        now = self._clock()
        slot = int(now // 60) * 60
        moment = datetime.fromtimestamp(slot)
        seen: set[str] = set()
        for plan in await self._repo(TestPlan).list():
            seen.add(plan.id)
            trigger = _decode_dict(plan.trigger_cfg)
            if str(trigger.get("type") or "manual") != "cron":
                continue
            expr = self._cron_expr_cached(plan.id, plan.updated_ts, trigger)
            if expr is None:
                continue
            cursor = self._cron_cursors.get(plan.id)
            if cursor is not None and slot <= cursor:
                continue
            if not expr.matches(moment):
                continue
            self._cron_cursors[plan.id] = slot
            try:
                result = await self.dispatch_plan(
                    plan.id, {"idempotency_key": f"cron:{plan.id}:{slot}"}
                )
            except Exception as error:  # noqa: BLE001
                self.ctx.logger.error(
                    "cron 触发派发失败（本槽跳过）：plan=%s slot=%s：%s", plan.id, slot, error
                )
                continue
            self._cron_dispatch_total += 1
            self.ctx.logger.info(
                "cron 触发派发：plan=%s slot=%s task=%s deduplicated=%s",
                plan.id,
                slot,
                result.get("task_id"),
                result.get("deduplicated"),
            )
        self._prune_cron_state(seen)

    def _cron_expr_cached(
        self, plan_id: str, updated_ts: float, trigger: Mapping[str, Any]
    ) -> CronExpr | None:
        """计划 cron 表达式解析（缓存按 ``updated_ts`` 失效，§5.1）。

        解析失败缓存错误并记一次 warning（同一 ``updated_ts`` 只报一次，
        防刷屏），返回 None 表示该计划本周期不可触发。
        """
        cached = self._cron_cache.get(plan_id)
        if cached is not None and cached[0] == updated_ts:
            return cached[1]
        expr: CronExpr | None = None
        error = ""
        raw = _as_mapping(trigger.get("config")).get("expr")
        try:
            if not isinstance(raw, str):
                raise ValueError(f"trigger.config.expr 需是字符串，收到 {raw!r}")
            expr = parse_cron(raw)
        except ValueError as exc:
            error = str(exc)
        self._cron_cache[plan_id] = (updated_ts, expr, error)
        if expr is None:
            self._warn(f"计划 {plan_id} cron 表达式非法，已跳过触发：{error}")
        return expr

    def _prune_cron_state(self, seen: set[str]) -> None:
        """裁剪已删计划的 cron 缓存 / 游标（防内存无界增长）。"""
        for store in (self._cron_cache, self._cron_cursors):
            for plan_id in [key for key in list(store) if key not in seen]:
                del store[plan_id]

    # ============================================================== 结果接收
    async def _on_job_result(self, payload: object, _meta: object) -> None:
        """``test.job.result`` 收件（comm 处理器，异步；异常隔离不外抛）。"""
        try:
            await self._handle_job_result(payload)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.error("处理 test.job.result 失败：%s", error)

    async def _handle_job_result(self, raw: object) -> None:
        """结果主路径：幂等 → 迟到 → proto 握手 → transform 链 → 落库聚合。"""
        if not isinstance(raw, Mapping):
            self._warn("test.job.result 载荷不是对象，忽略")
            return
        job_id = str(raw.get("job_id") or "")
        try:
            attempt = int(raw.get("attempt") or 0)
        except (TypeError, ValueError):
            attempt = 0
        if not job_id or attempt < 1:
            self._warn(f"test.job.result 缺 job_id/attempt，忽略：{raw!r}")
            return
        result_id = f"{job_id}:{attempt}"
        result_repo = self._repo(TestJobResult)
        if await result_repo.get(result_id) is not None:
            self._results_duplicate_total += 1
            self._warn(f"重复的结果上报（{result_id}），只计数不聚合")
            return
        job = await self._repo(TestJob).get(job_id)
        if job is None:
            self._warn(f"结果上报指向未知 job（{result_id}），忽略")
            return
        now = self._clock()
        proto = raw.get("proto")
        # 迟到结果：attempt 小于当前 —— 仍存结果行，不改 job 状态、不聚合（§5.2）
        if attempt < job.attempt:
            await result_repo.add(
                TestJobResult(
                    id=result_id,
                    job_id=job_id,
                    task_id=job.task_id,
                    attempt=attempt,
                    status=str(raw.get("status") or "failed"),
                    summary=_json_dumps(_decode_dict(raw.get("summary"))),
                    started_ts=float(raw.get("started_ts") or 0.0),
                    finished_ts=float(raw.get("finished_ts") or 0.0),
                    error=str(raw.get("error") or ""),
                    received_ts=now,
                )
            )
            self._late_results_total += 1
            self._warn(f"迟到的结果上报（{result_id}），落库但不聚合")
            return
        # proto 握手：不认识的版本记 error、不重试（§8.3）
        if proto != TEST_PROTO:
            error = f"proto mismatch: got {proto!r}, support {TEST_PROTO}"
            await result_repo.add(
                TestJobResult(
                    id=result_id,
                    job_id=job_id,
                    task_id=job.task_id,
                    attempt=attempt,
                    status="failed",
                    summary=_json_dumps({}),
                    started_ts=float(raw.get("started_ts") or 0.0),
                    finished_ts=float(raw.get("finished_ts") or 0.0),
                    error=error,
                    received_ts=now,
                )
            )
            if job.state in ("leased", "running"):
                await self._finish_job(job, "failed", summary={}, error=error, now=now)
            return

        result = _parse_job_result(raw)
        # result.transform：落库前归一化（可改 summary / cases）
        hook_ctx = HookCtx(
            chain=CHAIN_RESULT_TRANSFORM,
            result=result,
            meta={"job_id": job_id, "attempt": attempt, "task_id": job.task_id},
        )
        hook_ctx = await self._hooks.run_result_transform(hook_ctx)
        result = hook_ctx.result if hook_ctx.result is not None else result

        await result_repo.add(
            TestJobResult(
                id=result_id,
                job_id=job_id,
                task_id=job.task_id,
                attempt=attempt,
                status=result.status,
                summary=_json_dumps(dict(result.summary)),
                started_ts=result.started_ts,
                finished_ts=result.finished_ts,
                error=result.error,
                received_ts=now,
            )
        )
        if job.state not in ("leased", "running"):
            # 竞态（已终结 / 已 lost 后补报）：落库不聚合，同迟到口径
            self._late_results_total += 1
            self._warn(f"结果上报竞态（{result_id}，job={job.state}），落库但不聚合")
            return

        limit = self.config.case_results_limit
        cases = list(result.cases)
        truncated = len(cases) > limit
        for index, case in enumerate(cases[: max(0, limit)]):
            await self._repo(TestCaseResult).add(
                TestCaseResult(
                    id=f"{result_id}:{index}",
                    job_id=job_id,
                    task_id=job.task_id,
                    attempt=attempt,
                    case_id=case.case_id,
                    status=case.status,
                    duration_s=case.duration_s,
                    message=case.message,
                    detail_ref=case.detail_ref,
                )
            )
        stored_artifacts: list[tuple[TestArtifact, ArtifactRef]] = []
        art_repo = self._repo(TestArtifact)
        for ref in result.artifacts:
            artifact_id = ref.artifact_id or _new_id("a", now)
            if await art_repo.get(artifact_id) is not None:
                self._warn(f"产物 id 重复（{artifact_id}），跳过登记")
                continue
            row = TestArtifact(
                id=artifact_id,
                job_id=job_id,
                task_id=job.task_id,
                kind=ref.kind,
                name=ref.name,
                uri=ref.uri,
                size=ref.size,
                sha256=ref.sha256,
                state="local",
                created_ts=now,
            )
            await art_repo.add(row)
            stored_artifacts.append((row, ref))

        to = _JOB_RESULT_STATES.get(result.status, "failed")
        await self._update_job(
            job,
            to,
            reason=result.error or result.status,
            finished_ts=result.finished_ts or now,
            started_ts=result.started_ts or job.started_ts,
            error=result.error,
            result_seq=job.result_seq + 1,
        )
        await self._emit_job_finished(
            task_id=job.task_id,
            job_id=job.id,
            attempt=attempt,
            status=to,
            node_id=job.node_id,
            summary=dict(result.summary),
        )
        if _is_plan_job(job):
            if result.status == "succeeded":
                await self._expand_plan(job, result)
            # 分片清单就绪（或 plan 失败）：planning → running（§5.1）
            await self._leave_planning(job.task_id, reason="shard plan finished")
        failed = [
            {"case_id": case.case_id, "message": case.message, "detail_ref": case.detail_ref}
            for case in cases
            if case.status in ("failed", "error")
        ]
        if failed:
            await self._emit_case_finished(
                task_id=job.task_id,
                job_id=job.id,
                attempt=attempt,
                failed=failed[:_CASE_EVENT_LIMIT],
                truncated=truncated or len(failed) > _CASE_EVENT_LIMIT,
            )
        for row, ref in stored_artifacts:
            await self._emit_artifact_ready(
                artifact_id=row.id,
                task_id=job.task_id,
                job_id=job.id,
                kind=row.kind,
                uri=row.uri,
                size=row.size,
                sha256=row.sha256,
            )
            await self._run_artifact_sink(ref)
        await self._evaluate_barrier(job.task_id)

    async def _leave_planning(self, task_id: str, *, reason: str) -> None:
        """planning → running（分片清单就绪 / plan 收尾，§5.1）；已离开 planning 则不动。"""
        task = await self._repo(TestTask).get(task_id)
        if task is not None and task.state == "planning":
            await self._transition_task(task, "running", reason=reason)

    async def _expand_plan(self, job: TestJob, result: JobResult) -> None:
        """plan 结果 → 分片 job（§9.3）：shards 空/缺失建 1 个无分片 job。"""
        task = await self._repo(TestTask).get(job.task_id)
        stage = await self._repo(TestStage).get(job.stage_id)
        if task is None or stage is None:
            return
        raw_shards = result.summary.get("shards")
        shards = [item for item in (raw_shards or []) if isinstance(item, dict)]
        if not isinstance(raw_shards, list) or not shards:
            await self._create_job(task, stage, shard=None)
            return
        total = len(shards)
        for index, item in enumerate(shards):
            unit_refs = item.get("unit_refs")
            shard: dict[str, Any] = {
                "index": index,
                "total": total,
                "unit_refs": list(unit_refs) if isinstance(unit_refs, (list, tuple)) else [],
            }
            override = item.get("params")
            if isinstance(override, dict) and override:
                shard["params"] = dict(override)  # 分片级参数覆盖（派发时并入）
            await self._create_job(task, stage, shard=shard)

    async def _on_job_progress(self, payload: object, _meta: object) -> None:
        """``test.job.progress`` 收件（fire-and-forget，丢了无害）。"""
        try:
            await self._handle_job_progress(payload)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.error("处理 test.job.progress 失败：%s", error)

    async def _handle_job_progress(self, raw: object) -> None:
        """进度：首次记 started_ts、leased→running（§5.2）；note 无处存仅日志。"""
        if not isinstance(raw, Mapping):
            return
        job_id = str(raw.get("job_id") or "")
        job = await self._repo(TestJob).get(job_id)
        if job is None or job.state not in ("leased", "running"):
            return
        try:
            attempt = int(raw.get("attempt") or 0)
        except (TypeError, ValueError):
            attempt = 0
        if attempt and attempt != job.attempt:
            return  # 过期 attempt 的进度
        values: dict[str, object] = {}
        if job.started_ts <= 0:
            values["started_ts"] = self._clock()
        if job.state == "leased":
            values["state"] = "running"
        if values:
            await self._repo(TestJob).update(job.id, values)
        note = raw.get("note")
        if note:
            self.ctx.logger.info("job %s 进度：%s", job_id, note)

    def _on_node_status(self, payload: object) -> None:
        """``heartbeat/node-status``（emit）：同步监听器只投递 deque（§10）。"""
        if not isinstance(payload, Mapping):
            return
        node_id = str(payload.get("node_id") or "")
        to = str(payload.get("to") or "")
        if node_id and to in ("offline", "lost"):
            self._node_events.append(node_id)

    # ============================================================== 测试输出流
    async def _on_job_log(self, payload: object, meta: object) -> None:
        """``test.job.log`` 收件（fire-and-forget；comm 派发走 parallel，可 async）。"""
        try:
            await self._handle_job_log(payload, meta)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.error("处理 test.job.log 失败：%s", error)

    async def _handle_job_log(self, raw: object, meta: object) -> None:
        """日志流主路径：幂等排序 → 转发事件（载荷恒小）→ ``ctx.log`` 入库（§10.4）。

        幂等排序：每 ``(job_id, attempt)`` 维护已收最大 seq 游标——重复/乱序旧包
        丢弃并计 ``logs_duplicate_total``；跳号差值计 ``logs_lost_total``。
        """
        chunk = _parse_log_chunk(raw)
        if chunk is None:
            self._warn(f"test.job.log 载荷不合法，忽略：{raw!r}")
            return
        key = (chunk.job_id, chunk.attempt)
        cursor = self._log_cursors.get(key, -1)
        if chunk.seq <= cursor:
            self._logs_duplicate_total += 1
            self._warn(
                f"丢弃重复/乱序日志包（{key[0]} a{key[1]} seq={chunk.seq} ≤ {cursor}）"
            )
            return
        if chunk.seq > cursor + 1:
            self._logs_lost_total += chunk.seq - cursor - 1
        self._log_cursors[key] = chunk.seq
        job = await self._repo(TestJob).get(chunk.job_id)
        task_id = job.task_id if job is not None else ""
        # 转发：引用 + 少量行（裁剪至 _LOG_EVENT_LIMIT 行，载荷恒小）
        lines: list[dict[str, object]] = []
        for item in chunk.lines[:_LOG_EVENT_LIMIT]:
            line = _log_line(item)
            if line is not None:
                lines.append(line)
        try:
            await self.ctx.parallel(
                EVENT_JOB_LOG,
                {
                    "schema_version": SCHEMA_VERSION,
                    **({"task_id": task_id} if task_id else {}),
                    "job_id": chunk.job_id,
                    "attempt": chunk.attempt,
                    "seq": chunk.seq,
                    "stream": chunk.stream,
                    "case_id": chunk.case_id,
                    "lines": lines,
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error(EVENT_JOB_LOG, exc)
        # 入库：条目形状对齐 log 插件出口（ts/name/level/op/msg/source），
        # 追踪元组 op=task_id / name=test.{job_id}.{attempt} / source=case_id 或 stream
        log = self.ctx.get("log")
        if log is None:
            self.ctx.logger.debug("log 插件缺席，test.job.log 只转发不入库")
            return
        entries: list[dict[str, object]] = []
        for item in chunk.lines:
            line = _log_line(item)
            if line is None:
                continue
            entries.append(
                {
                    "ts": line["ts"],
                    "name": f"test.{chunk.job_id}.{chunk.attempt}",
                    "level": line["level"],
                    "op": task_id,
                    "msg": line["msg"],
                    "source": chunk.case_id or chunk.stream,
                }
            )
        if entries:
            log.ingest_remote({"logs": entries}, _ingest_meta(meta, self.ctx))

    # ============================================================== 屏障与收尾
    async def _finish_job(
        self,
        job: TestJob,
        to: str,
        *,
        summary: Mapping[str, Any],
        error: str,
        now: float | None = None,
    ) -> None:
        """job 终结：写行 + emit test/job-finished + Stage 屏障评估。"""
        at = self._clock() if now is None else now
        await self._update_job(
            job,
            to,
            reason=error or to,
            finished_ts=at,
            error=error,
        )
        await self._emit_job_finished(
            task_id=job.task_id,
            job_id=job.id,
            attempt=job.attempt,
            status=to,
            node_id=job.node_id,
            summary=dict(summary),
        )
        await self._evaluate_barrier(job.task_id)

    async def _evaluate_barrier(self, task_id: str) -> None:
        """Stage 屏障（§5.2）：Stage 内全部 job 终结 → 判定并推进 / 收尾。"""
        task = await self._repo(TestTask).get(task_id)
        if task is None or task.state in TASK_FINISHED:
            return
        jobs = await self._job_rows(task_id)
        if any(job.state not in JOB_FINISHED for job in jobs):
            return
        if task.state == "canceling":
            await self._finish_cancel(task)
            return
        # 安全兜底：plan 收尾事件丢失（恢复 / 竞态）时补走 planning → running
        await self._leave_planning(task_id, reason="shard plan finished")
        task = await self._repo(TestTask).get(task_id)
        if task is None or task.state in TASK_FINISHED:
            return
        stages = await self._stage_rows(task_id)
        current = next((s for s in stages if s.name == task.current_stage), None)
        if current is None:
            return
        stage_jobs = [job for job in jobs if job.stage_id == current.id]
        succeeded = bool(stage_jobs) and all(job.state == "succeeded" for job in stage_jobs)
        await self._repo(TestStage).update(
            current.id, {"state": "succeeded" if succeeded else "failed"}
        )
        if not succeeded and task.failure_policy == "fail_fast":
            await self._finish_task(task, "failed", reason=f"stage {current.name} failed")
            return
        later = [stage for stage in stages if stage.seq > current.seq]
        if later:
            # "continue"：无论成败推进下一 Stage（running 态不变，无迁移事件）
            await self._start_stage(task, later[0])
            return
        all_succeeded = all(
            stage.state == "succeeded" for stage in stages if stage.id != current.id
        ) and succeeded
        await self._finish_task(
            task, "succeeded" if all_succeeded else "failed", reason="all stages finished"
        )

    async def _finish_task(self, task: TestTask, to: str, *, reason: str) -> None:
        """任务终结：状态迁移 + 聚合计数 + emit test/task-finished（§11.1）。"""
        now = self._clock()
        counts = await self._aggregate_counts(task.id)
        await self._transition_task(task, to, reason=reason)
        await self._emit_task_finished(
            task_id=task.id,
            state=to,
            counts=counts,
            duration_s=max(0.0, now - task.created_ts),
        )

    async def _finish_cancel(self, task: TestTask, *, now: float | None = None) -> None:
        """取消收口：canceling → canceled + emit test/task-finished（幂等）。

        纯排队任务的收口可能被 ``_cancel_queued_job`` 的屏障评估与 ``cancel()``
        的兜底各触发一次；已离开 canceling（收口完成 / 已终结）即静默返回，
        保证 task_history 与 task-finished 事件只记一次。
        """
        fresh = await self._repo(TestTask).get(task.id)
        if fresh is None or fresh.state != "canceling":
            return
        at = self._clock() if now is None else now
        counts = await self._aggregate_counts(fresh.id)
        await self._transition_task(fresh, "canceled", reason="canceled")
        await self._emit_task_finished(
            task_id=fresh.id,
            state="canceled",
            counts=counts,
            duration_s=max(0.0, at - fresh.created_ts),
        )

    async def _aggregate_counts(self, task_id: str) -> dict[str, float]:
        """聚合计数：每 job 最新 attempt 结果的 summary 数值键求和。"""
        counts: dict[str, float] = {}
        for job in await self._job_rows(task_id):
            row = await self._repo(TestJobResult).get(f"{job.id}:{job.attempt}")
            if row is None:
                continue
            for key, value in _decode_dict(row.summary).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    counts[key] = counts.get(key, 0.0) + float(value)
        return {
            key: int(value) if float(value).is_integer() else value
            for key, value in counts.items()
        }

    # ============================================================== 查询门面
    async def get_task(self, task_id: str) -> Mapping[str, Any] | None:
        """任务详情（含 stages 与 extensions 收集槽）；不存在返回 None。"""
        task = await self._repo(TestTask).get(task_id)
        if task is None:
            return None
        detail: dict[str, Any] = self._task_dict(task)
        detail["stages"] = [self._stage_dict(s) for s in await self._stage_rows(task_id)]
        # extensions 收集槽（同 heartbeat collect-node 模式，§11.3）
        detail["extensions"] = {}
        detail["extension_errors"] = []
        try:
            await self.ctx.parallel(EVENT_COLLECT_TASK, detail)
        except EventDispatchError as exc:
            detail["extension_errors"] = [
                f"{type(error).__name__}: {error}" for error in exc.errors
            ]
        return detail

    async def list_tasks(
        self,
        *,
        state: str | None = None,
        labels: Mapping[str, str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        """任务列表（state 精确过滤、labels 子集匹配、分页）。"""
        where = TestTask.state == state if state else None
        rows = await self._repo(TestTask).list(where=where, order_by=TestTask.created_ts)
        wanted = dict(labels or {})
        if wanted:
            rows = [
                row
                for row in rows
                if all(_decode_dict(row.labels).get(k) == v for k, v in wanted.items())
            ]
        start = max(0, offset)
        end = start + max(0, limit)
        return [self._task_dict(row) for row in rows[start:end]]

    async def list_jobs(self, task_id: str) -> list[Mapping[str, Any]]:
        """任务的 Job 列表（含各 attempt 的结果历史）。"""
        result_repo = self._repo(TestJobResult)
        out: list[Mapping[str, Any]] = []
        for job in await self._job_rows(task_id):
            entry = self._job_dict(job)
            attempts = await result_repo.list(
                where=TestJobResult.job_id == job.id, order_by=TestJobResult.attempt
            )
            entry["results"] = [self._result_dict(row) for row in attempts]
            out.append(entry)
        return out

    async def list_task_history(
        self, task_id: str, *, limit: int = 200
    ) -> list[Mapping[str, Any]]:
        """任务状态迁移时间线（按 ts/id 排序，最多 limit 条）。"""
        rows = await self._repo(TestTaskHistory).list(
            where=TestTaskHistory.task_id == task_id,
            order_by=[TestTaskHistory.ts, TestTaskHistory.id],
        )
        entries: list[Mapping[str, Any]] = [
            {
                "id": row.id,
                "task_id": row.task_id,
                "from_state": row.from_state,
                "to_state": row.to_state,
                "reason": row.reason,
                "ts": row.ts,
            }
            for row in rows
        ]
        return entries[: max(0, int(limit))]

    async def get_result(self, task_id: str) -> Mapping[str, Any] | None:
        """聚合结果：summary 数值键求和 + jobs 明细 + cases / artifacts 计数。"""
        task = await self._repo(TestTask).get(task_id)
        if task is None:
            return None
        jobs = await self._job_rows(task_id)
        counts: dict[str, float] = {}
        details: list[dict[str, Any]] = []
        latest: set[str] = set()
        result_repo = self._repo(TestJobResult)
        for job in jobs:
            row = await result_repo.get(f"{job.id}:{job.attempt}")
            latest.add(f"{job.id}:{job.attempt}")
            summary = _decode_dict(row.summary) if row is not None else {}
            for key, value in summary.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    counts[key] = counts.get(key, 0.0) + float(value)
            details.append(
                {
                    "job_id": job.id,
                    "attempt": job.attempt,
                    "status": row.status if row is not None else job.state,
                    "node_id": job.node_id,
                    "summary": summary,
                }
            )
        case_counts: dict[str, int] = {}
        for row in await self._repo(TestCaseResult).list(
            where=TestCaseResult.task_id == task_id
        ):
            if f"{row.job_id}:{row.attempt}" in latest:
                case_counts[row.status] = case_counts.get(row.status, 0) + 1
        artifacts = await self._repo(TestArtifact).count(where=TestArtifact.task_id == task_id)
        finished = task.finished_ts if task.finished_ts > 0 else self._clock()
        return {
            "task_id": task_id,
            "state": task.state,
            "created_ts": task.created_ts,
            "finished_ts": task.finished_ts,
            "duration_s": max(0.0, finished - task.created_ts),
            "counts": {
                key: int(value) if float(value).is_integer() else value
                for key, value in counts.items()
            },
            "jobs": details,
            "cases": case_counts,
            "artifacts": artifacts,
        }

    async def list_case_results(
        self,
        *,
        task_id: str = "",
        job_id: str = "",
        status: str = "",
        limit: int = 200,
    ) -> list[Mapping[str, Any]]:
        """用例结果查询（task_id / job_id / status 过滤）。"""
        conditions = []
        if task_id:
            conditions.append(TestCaseResult.task_id == task_id)
        if job_id:
            conditions.append(TestCaseResult.job_id == job_id)
        if status:
            conditions.append(TestCaseResult.status == status)
        where = None
        for condition in conditions:
            where = condition if where is None else (where & condition)
        rows = await self._repo(TestCaseResult).list(where=where, limit=max(0, limit))
        return [
            {
                "id": row.id,
                "job_id": row.job_id,
                "task_id": row.task_id,
                "attempt": row.attempt,
                "case_id": row.case_id,
                "status": row.status,
                "duration_s": row.duration_s,
                "message": row.message,
                "detail_ref": row.detail_ref,
            }
            for row in rows
        ]

    async def list_artifacts(
        self, *, task_id: str = "", state: str = "local"
    ) -> list[Mapping[str, Any]]:
        """产物列表（state 过滤，默认本地产物）。"""
        conditions = []
        if task_id:
            conditions.append(TestArtifact.task_id == task_id)
        if state:
            conditions.append(TestArtifact.state == state)
        where = None
        for condition in conditions:
            where = condition if where is None else (where & condition)
        rows = await self._repo(TestArtifact).list(where=where, order_by=TestArtifact.created_ts)
        return [
            {
                "artifact_id": row.id,
                "job_id": row.job_id,
                "task_id": row.task_id,
                "kind": row.kind,
                "name": row.name,
                "uri": row.uri,
                "size": row.size,
                "sha256": row.sha256,
                "state": row.state,
                "created_ts": row.created_ts,
            }
            for row in rows
        ]

    async def mark_artifact(
        self, artifact_id: str, *, uri: str | None = None, state: str | None = None
    ) -> bool:
        """产物状态 / URI 回填（artifact_oss / artifact_gc 专用写口）；不存在返回 False。"""
        repo = self._repo(TestArtifact)
        if await repo.get(artifact_id) is None:
            return False
        values: dict[str, object] = {}
        if uri is not None:
            values["uri"] = uri
        if state is not None:
            values["state"] = state
        if values:
            await repo.update(artifact_id, values)
        return True

    # ============================================================== 行 → dict
    def _task_dict(self, row: TestTask) -> dict[str, Any]:
        return {
            "task_id": row.id,
            "idempotency_key": row.idempotency_key,
            "source": row.source,
            "name": row.name,
            "created_by": row.created_by,
            "priority": row.priority,
            "labels": _decode_dict(row.labels),
            "params": _public_params(_decode_dict(row.params)),
            "state": row.state,
            "current_stage": row.current_stage,
            "failure_policy": row.failure_policy,
            "error": row.error,
            "created_ts": row.created_ts,
            "updated_ts": row.updated_ts,
            "finished_ts": row.finished_ts,
        }

    def _stage_dict(self, row: TestStage) -> dict[str, Any]:
        return {
            "id": row.id,
            "task_id": row.task_id,
            "seq": row.seq,
            "name": row.name,
            "kind": row.kind,
            "executor": _decode_dict(row.executor),
            "params": _decode_dict(row.params),
            "shard_mode": row.shard_mode,
            "shard_hint": row.shard_hint,
            "requires": _decode_dict(row.requires),
            "state": row.state,
            "timeout_s": row.timeout_s,
        }

    def _job_dict(self, row: TestJob) -> dict[str, Any]:
        return {
            "job_id": row.id,
            "stage_id": row.stage_id,
            "task_id": row.task_id,
            "node_id": row.node_id,
            "attempt": row.attempt,
            "state": row.state,
            "shard": _decode_json(row.shard, None),
            "lease_ts": row.lease_ts,
            "started_ts": row.started_ts,
            "finished_ts": row.finished_ts,
            "error": row.error,
        }

    def _result_dict(self, row: TestJobResult) -> dict[str, Any]:
        return {
            "id": row.id,
            "job_id": row.job_id,
            "attempt": row.attempt,
            "status": row.status,
            "summary": _decode_dict(row.summary),
            "started_ts": row.started_ts,
            "finished_ts": row.finished_ts,
            "error": row.error,
            "received_ts": row.received_ts,
        }

    # ============================================================== HookChain
    def register_hook(
        self,
        chain: str,
        fn: Callable[..., Any],
        *,
        phase: str,
        order: int,
        name: str,
        on_error: str = "fail",
    ) -> Callable[[], None]:
        """登记拦截器，返回注销函数（注销绑定调用方 fiber 生命周期）。"""
        strategies = {"fail": OnError.FAIL, "skip": OnError.SKIP}
        if on_error not in strategies:
            raise ValueError(f"on_error 非法，收到 {on_error!r}（可选：fail / skip）")
        dispose = self._hooks.register(
            chain, fn, phase=phase, order=order, name=name, on_error=strategies[on_error]
        )
        # execute 的返回值即清理函数：随调用方 fiber 卸载自动注销
        self.ctx.effect(lambda: dispose, f"test_core.register_hook({name!r})")
        return dispose

    async def _run_artifact_sink(self, ref: ArtifactRef) -> None:
        """artifact.sink 旁路链：只读 ref，钩子错误不影响主流程（§11.2）。"""
        try:
            await self._hooks.run_artifact_sink(
                HookCtx(chain=CHAIN_ARTIFACT_SINK, ref=ref)
            )
        except Exception as error:  # noqa: BLE001
            self._warn(f"artifact.sink 钩子出错（已忽略）：{error}")

    # ============================================================== 事件派发
    async def _emit_task_created(
        self, *, task_id: str, name: str, labels: Mapping[str, str], source: str, priority: int
    ) -> None:
        try:
            await self.ctx.parallel(
                EVENT_TASK_CREATED,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "name": name,
                    "labels": dict(labels),
                    "source": source,
                    "priority": priority,
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error("test/task-created", exc)

    async def _emit_task_state(
        self, *, task_id: str, from_state: str, to: str, reason: str, at: float
    ) -> None:
        try:
            await self.ctx.parallel(
                EVENT_TASK_STATE,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "from": from_state,
                    "to": to,
                    "reason": reason,
                    "at": at,
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error("test/task-state", exc)

    async def _emit_task_finished(
        self, *, task_id: str, state: str, counts: Mapping[str, Any], duration_s: float
    ) -> None:
        try:
            await self.ctx.parallel(
                EVENT_TASK_FINISHED,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "state": state,
                    "counts": dict(counts),
                    "duration_s": duration_s,
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error("test/task-finished", exc)

    async def _emit_job_finished(
        self,
        *,
        task_id: str,
        job_id: str,
        attempt: int,
        status: str,
        node_id: str,
        summary: Mapping[str, Any],
    ) -> None:
        try:
            await self.ctx.parallel(
                EVENT_JOB_FINISHED,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "job_id": job_id,
                    "attempt": attempt,
                    "status": status,
                    "node_id": node_id,
                    "summary": dict(summary),
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error("test/job-finished", exc)

    async def _emit_case_finished(
        self,
        *,
        task_id: str,
        job_id: str,
        attempt: int,
        failed: Sequence[Mapping[str, str]],
        truncated: bool,
    ) -> None:
        try:
            await self.ctx.parallel(
                EVENT_CASE_FINISHED,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "job_id": job_id,
                    "attempt": attempt,
                    "failed": [dict(item) for item in failed],
                    "truncated": truncated,
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error("test/case-finished", exc)

    async def _emit_artifact_ready(
        self,
        *,
        artifact_id: str,
        task_id: str,
        job_id: str,
        kind: str,
        uri: str,
        size: int,
        sha256: str,
    ) -> None:
        try:
            await self.ctx.parallel(
                EVENT_ARTIFACT_READY,
                {
                    "schema_version": SCHEMA_VERSION,
                    "artifact_id": artifact_id,
                    "task_id": task_id,
                    "job_id": job_id,
                    "kind": kind,
                    "uri": uri,
                    "size": size,
                    "sha256": sha256,
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error("test/artifact-ready", exc)

    def _log_dispatch_error(self, event: str, exc: EventDispatchError) -> None:
        """派发失败不打断主流程，只记 warning（parallel 已隔离监听器异常）。"""
        self._warn(
            f"事件 {event} 派发有监听器出错："
            + "；".join(f"{type(error).__name__}: {error}" for error in exc.errors)
        )

    # ============================================================== 可观测性
    async def _refresh_gauges(self) -> None:
        """刷新 /healthz 仪表缓存（扫描循环定期调用）。"""
        self._gauge_inflight_jobs = len(await self._scheduler.inflight_jobs())
        self._gauge_running_tasks = await self._repo(TestTask).count(
            where=TestTask.state == "running"
        )
        self._gauge_materials_total = await self._repo(TestMaterial).count()

    def health_metrics(self) -> dict[str, object]:
        """/healthz 指标：队列 / 在途 / 运行任务与累计计数（只读）。"""
        return {
            **self._scheduler.metrics(),
            "inflight_jobs": self._gauge_inflight_jobs,
            "running_tasks": self._gauge_running_tasks,
            "materials_total": self._gauge_materials_total,
            "results_duplicate_total": self._results_duplicate_total,
            "late_results_total": self._late_results_total,
            "logs_lost_total": self._logs_lost_total,
            "logs_duplicate_total": self._logs_duplicate_total,
            "cron_dispatch_total": self._cron_dispatch_total,
            "hooks": self._hooks.stats(),
        }

    def _warn(self, message: str) -> None:
        self.ctx.logger.warning("%s", message)


# ============================================================== 载荷解析辅助
def _executor_of(stage: TestStage) -> ExecutorSelector:
    """stage.executor JSON → ExecutorSelector（坏行 / 旧行降级默认值，§3.2 缺省口径）。

    ``nodes`` 取出去重保序的非空台架名（空 = 不指定）；``strategy`` 只认
    ``"random"``，其余（含旧行缺失）按 ``"balanced"``（设计 §4）。
    """
    data = _decode_dict(stage.executor)
    labels = data.get("labels")
    nodes: tuple[str, ...] = ()
    nodes_raw = data.get("nodes")
    if isinstance(nodes_raw, list):
        kept: list[str] = []
        for item in nodes_raw:
            text = str(item or "")
            if text and text not in kept:
                kept.append(text)
        nodes = tuple(kept)
    strategy: Literal["balanced", "random"] = (
        "random" if data.get("strategy") == "random" else "balanced"
    )
    return ExecutorSelector(
        id=str(data.get("id") or ""),
        kind=str(data.get("kind") or "executor"),
        labels=dict(labels) if isinstance(labels, dict) else {},
        nodes=nodes,
        strategy=strategy,
    )


def _job_params(
    task: TestTask, stage: TestStage, shard: object
) -> dict[str, Any]:
    """派发参数：任务级（注入各 Stage）→ Stage 参数 → 分片 params 覆盖。"""
    params: dict[str, Any] = _public_params(_decode_dict(task.params))
    params.update(_decode_dict(stage.params))
    if isinstance(shard, dict):
        override = shard.get("params")
        if isinstance(override, dict):
            params.update(override)
    return params


def _descriptor_shard(shard: object) -> dict[str, Any] | None:
    """JobDescriptor.shard：与 shard 列同形（分片 job 去掉内部 params 键）。"""
    if not isinstance(shard, dict):
        return None
    return {
        key: value for key, value in shard.items() if key != "params"
    }


def _is_plan_job(job: TestJob) -> bool:
    """是否 plan 型 job（shard 列存 {"plan": true}，§9.3）。"""
    shard = _decode_json(job.shard, None)
    return isinstance(shard, dict) and bool(shard.get("plan"))


def _parse_job_result(raw: Mapping[str, Any]) -> JobResult:
    """上行载荷 → JobResult（忽略未知字段、缺省给默认值，§3.2）。"""
    cases: list[CaseResult] = []
    for item in raw.get("cases") or []:
        if isinstance(item, Mapping):
            cases.append(
                CaseResult(
                    case_id=str(item.get("case_id") or ""),
                    status=str(item.get("status") or "failed"),  # type: ignore[arg-type]
                    duration_s=float(item.get("duration_s") or 0.0),
                    message=str(item.get("message") or ""),
                    detail_ref=str(item.get("detail_ref") or ""),
                )
            )
    artifacts: list[ArtifactRef] = []
    for item in raw.get("artifacts") or []:
        if isinstance(item, Mapping):
            artifacts.append(
                ArtifactRef(
                    artifact_id=str(item.get("artifact_id") or ""),
                    kind=str(item.get("kind") or "attachment"),  # type: ignore[arg-type]
                    name=str(item.get("name") or ""),
                    uri=str(item.get("uri") or ""),
                    size=int(item.get("size") or 0),
                    sha256=str(item.get("sha256") or ""),
                )
            )
    status = str(raw.get("status") or "failed")
    if status not in ("succeeded", "failed", "canceled", "timeout"):
        status = "failed"
    return JobResult(
        proto=int(raw.get("proto") or 0),
        job_id=str(raw.get("job_id") or ""),
        attempt=int(raw.get("attempt") or 0),
        status=status,  # type: ignore[arg-type]
        summary=_decode_dict(raw.get("summary")),
        cases=cases,
        artifacts=artifacts,
        started_ts=float(raw.get("started_ts") or 0.0),
        finished_ts=float(raw.get("finished_ts") or 0.0),
        error=str(raw.get("error") or ""),
    )


__all__: list[str] = ["TestCoreInterface", "TestCorePlugin"]
