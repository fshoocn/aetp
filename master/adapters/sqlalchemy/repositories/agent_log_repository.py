"""SQLAlchemy Agent 结构化日志仓储。"""

from __future__ import annotations

from datetime import datetime

from aetp_protocol.ids import BusinessId, SessionId
from aetp_protocol.logs import LogEvent
from sqlalchemy import select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import AgentLogEvent as AgentLogEventORM
from master.domain.models import AgentLogEventRecord
from master.domain.repositories import AgentLogRepository


def _to_domain(orm: AgentLogEventORM) -> AgentLogEventRecord:
    event = LogEvent.model_validate(orm.event)
    if (
        event.event_id.root != orm.event_id
        or event.sequence != orm.sequence
        or event.source_id != orm.source_id
        or event.component != orm.component
        or event.event_code.root != orm.event_code
    ):
        raise ValueError("Agent 日志控制字段与事件快照不一致")
    return AgentLogEventRecord(
        id=orm.id,
        node_id=BusinessId(orm.node_id),
        session_id=SessionId(orm.session_id),
        sequence=orm.sequence,
        event=event,
        batch_first_sequence=orm.batch_first_sequence,
        received_at=orm.received_at,
        created_at=orm.created_at,
    )


class AgentLogRepositoryImpl(AgentLogRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def existing_sequences(
        self,
        node_id: BusinessId,
        session_id: str,
        sequences: list[int],
    ) -> set[int]:
        if not sequences:
            return set()
        return set(
            self._s.execute(
                select(AgentLogEventORM.sequence).where(
                    AgentLogEventORM.node_id == node_id.root,
                    AgentLogEventORM.session_id == session_id,
                    AgentLogEventORM.sequence.in_(sequences),
                )
            )
            .scalars()
            .all()
        )

    def add_many(self, records: list[AgentLogEventRecord]) -> list[AgentLogEventRecord]:
        for record in records:
            event = record.event
            context = event.context.model_dump(mode="json", exclude_none=True)
            self._s.add(
                AgentLogEventORM(
                    node_id=record.node_id.root,
                    session_id=record.session_id.root,
                    event_id=event.event_id.root,
                    source=event.source,
                    source_id=event.source_id,
                    sequence=event.sequence,
                    occurred_at=event.occurred_at,
                    level=event.level.value,
                    component=event.component,
                    event_code=event.event_code.root,
                    message_template=event.message_template,
                    message=event.message,
                    project_id=(event.context.project_id.root if event.context.project_id is not None else None),
                    run_id=(event.context.run_id.root if event.context.run_id is not None else None),
                    attempt_id=(event.context.attempt_id.root if event.context.attempt_id is not None else None),
                    plan_id=(event.context.plan_id.root if event.context.plan_id is not None else None),
                    plugin_id=(event.context.plugin_id.root if event.context.plugin_id is not None else None),
                    context=context,
                    detail=dict(event.detail),
                    exception=(
                        event.exception.model_dump(mode="json", exclude_none=True)
                        if event.exception is not None
                        else None
                    ),
                    event=event.model_dump(mode="json"),
                    batch_first_sequence=record.batch_first_sequence,
                    received_at=record.received_at,
                )
            )
        self._s.flush()
        return [
            _to_domain(
                self._s.execute(
                    select(AgentLogEventORM).where(
                        AgentLogEventORM.node_id == record.node_id.root,
                        AgentLogEventORM.session_id == record.session_id.root,
                        AgentLogEventORM.sequence == record.sequence,
                    )
                ).scalar_one()
            )
            for record in records
        ]

    def list(
        self,
        node_id: BusinessId,
        *,
        session_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
        level: str | None = None,
        component: str | None = None,
        event_code: str | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
        plugin_id: str | None = None,
        keyword: str | None = None,
        occurred_after: datetime | None = None,
        occurred_before: datetime | None = None,
    ) -> list[AgentLogEventRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("日志查询 limit 必须在 1..1000 范围内")
        statement = select(AgentLogEventORM).where(
            AgentLogEventORM.node_id == node_id.root,
            AgentLogEventORM.sequence > max(0, after_sequence),
        )
        if session_id is not None:
            statement = statement.where(AgentLogEventORM.session_id == session_id)
        if level is not None:
            statement = statement.where(AgentLogEventORM.level == level)
        if component is not None:
            statement = statement.where(AgentLogEventORM.component == component)
        if event_code is not None:
            statement = statement.where(AgentLogEventORM.event_code == event_code)
        if run_id is not None:
            statement = statement.where(AgentLogEventORM.run_id == run_id)
        if attempt_id is not None:
            statement = statement.where(AgentLogEventORM.attempt_id == attempt_id)
        if plugin_id is not None:
            statement = statement.where(AgentLogEventORM.plugin_id == plugin_id)
        if occurred_after is not None:
            statement = statement.where(AgentLogEventORM.occurred_at >= occurred_after)
        if occurred_before is not None:
            statement = statement.where(AgentLogEventORM.occurred_at <= occurred_before)
        if keyword:
            pattern = f"%{keyword}%"
            statement = statement.where(
                AgentLogEventORM.message.ilike(pattern)
                | AgentLogEventORM.message_template.ilike(pattern)
            )
        statement = statement.order_by(AgentLogEventORM.sequence).limit(limit)
        return [_to_domain(item) for item in self._s.execute(statement).scalars().all()]
