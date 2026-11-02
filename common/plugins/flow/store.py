"""flow 持久化（Phase 3 / §5.10）：**事件日志为主、行投影为辅**。

- 事件表 ``aetp_flow_events``：每个 run 一份 append-only 日志（run-created /
  run-started / node-attempt-started / node-attempt-finished / node-finished /
  run-cancel-requested / run-resume-requested / run-interrupted / run-finished），
  ``seq`` 为 run 内单调序号，含 actor / init_time / duration_ms 等事实；
- 行投影 ``aetp_flow_runs`` / ``aetp_flow_steps``：为查询 / 外部系统对接
  （ALM）而生，**每次与事件同事务更新**，可随时从事件重建（事实只认事件）；
- ``aetp_flow_defs``：用户流程定义（Phase 5 按 kind=builtin / user 落库），
  本阶段只交付表结构。

恢复约定（§5.10）：重启时从 run-created 载荷还原节点 / 覆盖 / 禁用集、从
steps 投影还原 blackboard 与 attempt 序号；进行中尝试不臆断结果（幂等步重放、
未知步标 unknown 并打断为 interrupted）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update

from common.flow_contracts import (
    Mode,
    NodeDef,
)
from common.models import Mapped, Model, mapped_column

#: run-created 事件的 payload 版本（节点序列化格式）
NODES_PAYLOAD_VERSION: int = 1


# --------------------------------------------------------------------------- #
# 模型（__owner__="flow" → 物理表 aetp_flow_*，标识符 ≤ 30 字符）
# --------------------------------------------------------------------------- #

class FlowDefRecord(Model):
    """流程定义行（``aetp_flow_defs``；Phase 5 写入，用户流程可设 enabled=0）。"""

    __owner__ = "flow"
    __tablename__ = "defs"

    id: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(index=True)  # builtin / user
    title: Mapped[str]
    extends: Mapped[str] = mapped_column(default="")  # 模板 id 或空
    body: Mapped[str]  # JSON 文本（完整流程定义）
    enabled: Mapped[int] = mapped_column(default=1)  # 0 = 仅管理员可见（已停用）
    needs_attention: Mapped[int] = mapped_column(default=0)  # 模板升级待校验
    created_by: Mapped[str] = mapped_column(default="")
    created_at: Mapped[float] = mapped_column(default=0.0)


class FlowEvent(Model):
    """run 事件日志（``aetp_flow_events``，append-only；(run_id, seq) 主键）。"""

    __owner__ = "flow"
    __tablename__ = "events"

    run_id: Mapped[str] = mapped_column(primary_key=True, index=True)
    seq: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(index=True)
    ts: Mapped[float]
    actor: Mapped[str] = mapped_column(default="")
    payload: Mapped[str] = mapped_column(default="")  # JSON 文本


class FlowRun(Model):
    """run 行投影（``aetp_flow_runs``；与事件同事务更新，可重建）。"""

    __owner__ = "flow"
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(primary_key=True)
    flow_id: Mapped[str] = mapped_column(index=True)
    flow_version: Mapped[int] = mapped_column(default=0)
    state: Mapped[str] = mapped_column(index=True)
    initiator: Mapped[str] = mapped_column(default="")
    #: 幂等键（可空 unique：多个无键 run 互不冲突，同键唯一——§5.10）
    idempotency_key: Mapped[str | None] = mapped_column(nullable=True, unique=True)
    input: Mapped[str] = mapped_column(default="")  # JSON
    overrides: Mapped[str] = mapped_column(default="")  # JSON
    bindings: Mapped[str] = mapped_column(default="")  # JSON（node → 绑定快照）
    error: Mapped[str] = mapped_column(default="")
    created_at: Mapped[float] = mapped_column(default=0.0)
    started_at: Mapped[float] = mapped_column(default=0.0)
    finished_at: Mapped[float] = mapped_column(default=0.0)


class FlowStep(Model):
    """步骤 attempt 行投影（``aetp_flow_steps``；(run_id, node_id, attempt) 主键）。"""

    __owner__ = "flow"
    __tablename__ = "steps"

    run_id: Mapped[str] = mapped_column(primary_key=True, index=True)
    node_id: Mapped[str] = mapped_column(primary_key=True)
    attempt: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column(default="")
    exec_node: Mapped[str] = mapped_column(default="")
    job_id: Mapped[str] = mapped_column(default="")
    output: Mapped[str] = mapped_column(default="")  # JSON
    error: Mapped[str] = mapped_column(default="")
    started_at: Mapped[float] = mapped_column(default=0.0)
    finished_at: Mapped[float] = mapped_column(default=0.0)


# --------------------------------------------------------------------------- #
# 节点序列化（run-created 事件的 payload；恢复还原用）
# --------------------------------------------------------------------------- #

def nodes_to_json(nodes: Sequence[NodeDef]) -> str:
    """NodeDef 全字段序列化（恢复还原用；谓词类字段不存在，皆为数据）。"""
    items = [
        {
            "id": node.id,
            "stage": node.stage,
            "labels": dict(node.labels),
            "pin": node.pin,
            "mode": node.mode.value,
            "required": node.required,
            "locked": node.locked,
            "always": node.always,
            "params": dict(node.params),
            "retries": node.retries,
            "backoff": node.backoff,
            "timeout": node.timeout,
        }
        for node in nodes
    ]
    return json.dumps(items, ensure_ascii=False)


def nodes_from_json(text: str) -> tuple[NodeDef, ...]:
    """节点载荷还原为 :class:`NodeDef` 元组（run-created 事件的逆变换）。"""
    data = json.loads(text or "[]")
    nodes: list[NodeDef] = []
    for item in data if isinstance(data, list) else []:
        nodes.append(
            NodeDef(
                id=str(item["id"]),
                stage=str(item["stage"]),
                labels=dict(item.get("labels") or {}),
                pin=item.get("pin"),
                mode=Mode(item.get("mode") or "one"),
                required=bool(item.get("required", True)),
                locked=bool(item.get("locked", False)),
                always=bool(item.get("always", False)),
                params=dict(item.get("params") or {}),
                retries=int(item.get("retries") or 0),
                backoff=float(item.get("backoff") or 1.0),
                timeout=item.get("timeout"),
            )
        )
    return tuple(nodes)


def overrides_to_dict(overrides: Any) -> dict[str, Any]:
    """Overrides（TypedDict：disable / insert / params / pin / labels）→ 纯 dict。"""
    return dict(overrides) if overrides else {}


def overrides_to_json(overrides: Any) -> str:
    return json.dumps(overrides_to_dict(overrides), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 存储门面（也是 executor 的 RunRecorder：每步 = 事件 append + 投影同事务）
# --------------------------------------------------------------------------- #

class FlowStore:
    """``ctx.db`` 上的事件日志 + 行投影门面（§5.10：事件为主、投影为辅）。"""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def sync(self) -> None:
        """建缺表（幂等）；只认领 flow 归属的四张表。"""
        await self._db.sync_schema(
            models=[FlowDefRecord, FlowEvent, FlowRun, FlowStep]
        )

    # -- 内部：事件 append（须在调用方事务内）------------------------------- #

    async def _append(
        self, session: Any, run_id: str, type_: str, payload: Mapping[str, Any],
        actor: str = "",
    ) -> int:
        row = (
            await session.execute(
                select(func.max(FlowEvent.seq)).where(FlowEvent.run_id == run_id)
            )
        ).first()
        seq = int(row[0] or 0) + 1
        await session.execute(
            sa_insert(FlowEvent).values(
                run_id=run_id, seq=seq, type=type_, ts=time.time(),
                actor=actor, payload=json.dumps(dict(payload), ensure_ascii=False),
            )
        )
        return seq

    # -- recorder 接口（executor 调用；每方法一个事务）--------------------- #

    async def record_run_created(self, run: Any, *, actor: str) -> None:
        now = time.time()
        async with self._db.transaction() as session:
            await self._append(
                session, run.id, "run-created",
                {
                    "run_id": run.id,
                    "flow_id": run.flow_id,
                    "flow_version": run.flow_version,
                    "initiator": run.initiator,
                    "idempotency_key": run.idempotency_key,
                    "input": dict(run.input),
                    "overrides": overrides_to_dict(run.overrides),
                    "nodes": nodes_to_json(run.nodes),
                    "disabled": sorted(run.disabled),
                    "init_time": now,
                },
                actor,
            )
            await session.execute(
                sa_insert(FlowRun).values(
                    id=run.id, flow_id=run.flow_id, flow_version=run.flow_version,
                    state="pending", initiator=run.initiator,
                    idempotency_key=run.idempotency_key or None,
                    input=json.dumps(run.input, ensure_ascii=False),
                    overrides=overrides_to_json(run.overrides),
                    bindings="{}", created_at=now,
                )
            )

    async def record_run_started(self, run: Any, *, actor: str = "") -> None:
        async with self._db.transaction() as session:
            await self._append(session, run.id, "run-started", {"run_id": run.id}, actor)
            await session.execute(
                sa_update(FlowRun)
                .where(FlowRun.id == run.id)
                .values(state="running", started_at=time.time())
            )

    async def record_attempt_started(
        self, run: Any, node_id: str, attempt: int, record: Any, exec_node: str,
    ) -> None:
        async with self._db.transaction() as session:
            await self._append(
                session, run.id, "node-attempt-started",
                {
                    "run_id": run.id, "node_id": node_id, "attempt": attempt,
                    "step": record.step_id, "provider": record.provider,
                    "version": record.version, "exec_node": exec_node,
                },
            )
            await session.execute(
                sa_insert(FlowStep).values(
                    run_id=run.id, node_id=node_id, attempt=attempt,
                    state="running", provider=record.provider, exec_node=exec_node,
                    started_at=time.time(),
                )
            )
            bindings = dict(getattr(run, "bindings", {}) or {})
            bindings[node_id] = {
                "provider": record.provider, "step": record.step_id,
                "version": record.version, "exec_node": exec_node,
                "idempotent": bool(record.idempotent),
            }
            if hasattr(run, "bindings"):
                run.bindings = bindings
            await session.execute(
                sa_update(FlowRun)
                .where(FlowRun.id == run.id)
                .values(bindings=json.dumps(bindings, ensure_ascii=False))
            )

    async def record_attempt_finished(
        self, run: Any, node_id: str, attempt: int, *,
        state: str, output: Any = None, error: str = "", duration_ms: float = 0.0,
    ) -> None:
        async with self._db.transaction() as session:
            await self._append(
                session, run.id, "node-attempt-finished",
                {
                    "run_id": run.id, "node_id": node_id, "attempt": attempt,
                    "state": state,
                    "output": output if output is None else dict(output),
                    "error": error, "duration_ms": duration_ms,
                },
            )
            await session.execute(
                sa_update(FlowStep)
                .where(
                    FlowStep.run_id == run.id,
                    FlowStep.node_id == node_id,
                    FlowStep.attempt == attempt,
                )
                .values(
                    state=state,
                    output=json.dumps(output or {}, ensure_ascii=False),
                    error=error, finished_at=time.time(),
                )
            )

    async def record_node_finished(self, run: Any, node_id: str, nr: Any) -> None:
        async with self._db.transaction() as session:
            await self._append(
                session, run.id, "node-finished",
                {
                    "run_id": run.id, "node_id": node_id,
                    "state": nr.state.value if hasattr(nr.state, "value") else str(nr.state),
                    "provider": nr.provider, "step": nr.step,
                    "attempts": nr.attempts, "reason": nr.reason, "error": nr.error,
                },
            )

    async def record_run_finished(self, run: Any) -> None:
        now = time.time()
        async with self._db.transaction() as session:
            await self._append(
                session, run.id, "run-finished",
                {
                    "run_id": run.id, "state": run.state,
                    "duration_ms": run.duration_ms(),
                    "warnings": list(run.warnings), "error": run.error or "",
                },
            )
            await session.execute(
                sa_update(FlowRun)
                .where(FlowRun.id == run.id)
                .values(
                    state=run.state, error=run.error or "",
                    finished_at=now,
                )
            )

    async def record_cancel_requested(
        self, run_id: str, *, reason: str = "", actor: str = ""
    ) -> None:
        async with self._db.transaction() as session:
            await self._append(
                session, run_id, "run-cancel-requested",
                {"run_id": run_id, "reason": reason}, actor,
            )

    async def record_resume_requested(
        self, run_id: str, *, mode: str, actor: str = ""
    ) -> None:
        async with self._db.transaction() as session:
            await self._append(
                session, run_id, "run-resume-requested",
                {"run_id": run_id, "mode": mode}, actor,
            )

    async def record_run_interrupted(
        self, run_id: str, *, node_id: str, reason: str, actor: str = "system"
    ) -> None:
        """崩溃恢复的「打断」：所有在途尝试标 unknown、run 进 interrupted（同事务）。"""
        async with self._db.transaction() as session:
            await self._append(
                session, run_id, "run-interrupted",
                {"run_id": run_id, "node_id": node_id, "reason": reason}, actor,
            )
            await session.execute(
                sa_update(FlowStep)
                .where(FlowStep.run_id == run_id, FlowStep.state == "running")
                .values(state="unknown", error=reason, finished_at=time.time())
            )
            await session.execute(
                sa_update(FlowRun)
                .where(FlowRun.id == run_id)
                .values(state="interrupted", error=reason)
            )

    # -- 查询（读侧；供 service / API）------------------------------------- #

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(FlowRun).where(FlowRun.id == run_id)
                )
            ).first()
        return _run_dict(row[0]) if row is not None else None

    async def get_run_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(FlowRun).where(FlowRun.idempotency_key == idempotency_key)
                )
            ).first()
        return _run_dict(row[0]) if row is not None else None

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(FlowEvent)
                    .where(FlowEvent.run_id == run_id)
                    .order_by(FlowEvent.seq.asc())
                )
            ).all()
        return [
            {
                "seq": row[0].seq, "type": row[0].type, "ts": row[0].ts,
                "actor": row[0].actor,
                "payload": json.loads(row[0].payload or "{}"),
            }
            for row in rows
        ]

    async def steps(self, run_id: str) -> list[dict[str, Any]]:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(FlowStep)
                    .where(FlowStep.run_id == run_id)
                    .order_by(FlowStep.attempt.asc())
                )
            ).all()
        return [_step_dict(row[0]) for row in rows]

    async def unfinished_runs(self) -> list[dict[str, Any]]:
        """恢复扫描：非终态 run（pending / running；interrupted 等人工处置）。"""
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(FlowRun).where(FlowRun.state.in_(("pending", "running")))
                )
            ).all()
        return [_run_dict(row[0]) for row in rows]

    async def list_runs(
        self, *, flow_id: str | None = None, state: str | None = None,
        from_ts: float | None = None, to_ts: float | None = None,
        limit: int = 20, offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """run 列表（新 → 旧）+ 总数（分页 + 时间窗用）。"""
        async with self._db.session() as session:
            statement = select(FlowRun)
            count_statement = select(func.count()).select_from(FlowRun)
            filters = (
                (FlowRun.flow_id == flow_id) if flow_id else None,
                (FlowRun.state == state) if state else None,
                (FlowRun.created_at >= from_ts) if from_ts else None,
                (FlowRun.created_at <= to_ts) if to_ts else None,
            )
            for filter_ in filters:
                if filter_ is not None:
                    statement = statement.where(filter_)
                    count_statement = count_statement.where(filter_)
            total = int(
                (await session.execute(count_statement)).first()[0] or 0
            )
            rows = (
                await session.execute(
                    statement.order_by(FlowRun.created_at.desc())
                    .limit(max(1, min(limit, 200))).offset(max(0, offset))
                )
            ).all()
        return [_run_dict(row[0]) for row in rows], total

    # -- 流程定义（Phase 5 / §5.14.4：不可变版本，kind=user）---------------- #

    async def save_def(
        self,
        flow_id: str,
        body: Mapping[str, Any],
        *,
        extends: str = "",
        actor: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        """保存用户流程新版本（不可变；version = 现有最大 + 1）。"""
        async with self._db.transaction() as session:
            row = (
                await session.execute(
                    select(func.max(FlowDefRecord.version)).where(
                        FlowDefRecord.id == flow_id
                    )
                )
            ).first()
            version = int(row[0] or 0) + 1
            await session.execute(
                sa_insert(FlowDefRecord).values(
                    id=flow_id, version=version, kind="user",
                    title=str(body.get("title") or ""),
                    extends=extends or "",
                    body=json.dumps(dict(body), ensure_ascii=False),
                    enabled=1 if enabled else 0,
                    needs_attention=0,
                    created_by=actor or "",
                    created_at=time.time(),
                )
            )
        saved = await self.get_def(flow_id, version)
        assert saved is not None
        return saved

    async def get_def(
        self, flow_id: str, version: int | None = None
    ) -> dict[str, Any] | None:
        """取定义行；``version`` 缺省取最新。"""
        async with self._db.session() as session:
            statement = select(FlowDefRecord).where(FlowDefRecord.id == flow_id)
            if version is not None:
                statement = statement.where(FlowDefRecord.version == version)
            else:
                statement = statement.order_by(FlowDefRecord.version.desc())
            row = (await session.execute(statement.limit(1))).first()
        return _def_dict(row[0]) if row is not None else None

    async def defs(self) -> list[dict[str, Any]]:
        """全部定义行（新 → 旧；缓存 / 重校验用）。"""
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(FlowDefRecord).order_by(
                        FlowDefRecord.id.asc(), FlowDefRecord.version.desc()
                    )
                )
            ).all()
        return [_def_dict(row[0]) for row in rows]

    async def set_def_flags(
        self, flow_id: str, version: int, *,
        needs_attention: int | None = None, enabled: int | None = None,
    ) -> None:
        """更新标记列（模板升级重校验 / 停用；body 不可变，只动标记）。"""
        values: dict[str, int] = {}
        if needs_attention is not None:
            values["needs_attention"] = needs_attention
        if enabled is not None:
            values["enabled"] = enabled
        if not values:
            return
        async with self._db.session() as session:
            await session.execute(
                sa_update(FlowDefRecord)
                .where(FlowDefRecord.id == flow_id, FlowDefRecord.version == version)
                .values(**values)
            )

    async def delete_def(self, flow_id: str) -> int:
        """删除用户流程的全部版本（历史 run 不受影响，§5.14.4）。"""
        async with self._db.session() as session:
            result = await session.execute(
                sa_delete(FlowDefRecord).where(FlowDefRecord.id == flow_id)
            )
            return int(result.rowcount or 0)

    async def retain(self, *, retain_days: float, max_runs: int) -> int:
        """保留双限（先到先删）：过期 run + 总数上限；删 run 时连带事件/步骤。"""
        async with self._db.transaction() as session:
            rows = (
                await session.execute(
                    select(FlowRun.id).where(
                        FlowRun.created_at < time.time() - retain_days * 86400.0
                    )
                )
            ).all()
            expired = [str(row[0]) for row in rows]
            keep = select(FlowRun.id).order_by(FlowRun.created_at.desc()).limit(
                max(1, max_runs)
            )
            rows = (
                await session.execute(select(FlowRun.id).where(FlowRun.id.not_in(keep)))
            ).all()
            doomed = sorted(set(expired) | {str(row[0]) for row in rows})
            for run_id in doomed:
                await session.execute(sa_delete(FlowEvent).where(FlowEvent.run_id == run_id))
                await session.execute(sa_delete(FlowStep).where(FlowStep.run_id == run_id))
                await session.execute(sa_delete(FlowRun).where(FlowRun.id == run_id))
        return len(doomed)


def _def_dict(record: FlowDefRecord) -> dict[str, Any]:
    return {
        "id": record.id, "version": record.version, "kind": record.kind,
        "title": record.title, "extends": record.extends,
        "body": json.loads(record.body or "{}"),
        "enabled": bool(record.enabled), "needs_attention": bool(record.needs_attention),
        "created_by": record.created_by, "created_at": record.created_at,
    }


def _run_dict(run: FlowRun) -> dict[str, Any]:
    return {
        "id": run.id, "flow_id": run.flow_id, "flow_version": run.flow_version,
        "state": run.state, "initiator": run.initiator,
        "idempotency_key": run.idempotency_key or "",
        "input": json.loads(run.input or "{}"),
        "overrides": json.loads(run.overrides or "{}"),
        "bindings": json.loads(run.bindings or "{}"),
        "error": run.error, "created_at": run.created_at,
        "started_at": run.started_at, "finished_at": run.finished_at,
    }


def _step_dict(step: FlowStep) -> dict[str, Any]:
    return {
        "run_id": step.run_id, "node_id": step.node_id, "attempt": step.attempt,
        "state": step.state, "provider": step.provider, "exec_node": step.exec_node,
        "job_id": step.job_id, "output": json.loads(step.output or "{}"),
        "error": step.error, "started_at": step.started_at,
        "finished_at": step.finished_at,
    }


__all__: list[str] = [
    "NODES_PAYLOAD_VERSION",
    "FlowDefRecord",
    "FlowEvent",
    "FlowRun",
    "FlowStep",
    "FlowStore",
    "nodes_from_json",
    "nodes_to_json",
    "overrides_to_dict",
    "overrides_to_json",
]
