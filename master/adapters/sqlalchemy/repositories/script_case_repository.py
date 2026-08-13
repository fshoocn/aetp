"""SQLAlchemy 脚本用例仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import ScriptCase as ScriptCaseORM
from master.adapters.sqlalchemy.orm import TestScript as TestScriptORM
from master.domain.models import ScriptCase
from master.domain.repositories import ScriptCaseRepository


def _to_domain(orm: ScriptCaseORM) -> ScriptCase:
    return ScriptCase(
        id=orm.id,
        script_id=orm.script.script_id if orm.script is not None else "",
        case_id=orm.case_id,
        stable_key=orm.stable_key,
        name=orm.name,
        parent_path=orm.parent_path,
        tags=list(orm.tags or []),
        params=dict(orm.params or {}),
        avg_duration_s=orm.avg_duration_s,
        duration_samples=orm.duration_samples,
        order_index=orm.order_index,
        deleted=orm.deleted,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class ScriptCaseRepositoryImpl(ScriptCaseRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def list_by_script(
        self, script_id: str, *, include_deleted: bool = False
    ) -> list[ScriptCase]:
        stmt = (
            select(ScriptCaseORM)
            .options(joinedload(ScriptCaseORM.script))
            .where(
                ScriptCaseORM.script_pk
                == select(TestScriptORM.id)
                .where(TestScriptORM.script_id == script_id)
                .scalar_subquery()
            )
            .order_by(ScriptCaseORM.order_index, ScriptCaseORM.id)
        )
        if not include_deleted:
            stmt = stmt.where(ScriptCaseORM.deleted.is_(False))
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def get_by_stable_key(
        self, script_id: str, stable_key: str
    ) -> ScriptCase | None:
        orm = self._s.execute(
            select(ScriptCaseORM)
            .options(joinedload(ScriptCaseORM.script))
            .where(
                ScriptCaseORM.script_pk
                == select(TestScriptORM.id)
                .where(TestScriptORM.script_id == script_id)
                .scalar_subquery(),
                ScriptCaseORM.stable_key == stable_key,
            )
        ).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def add(self, case: ScriptCase) -> ScriptCase:
        return self.add_many([case])[0]

    def add_many(self, cases: list[ScriptCase]) -> list[ScriptCase]:
        first_script_id = cases[0].script_id if cases else ""
        script_pk = None
        for case in cases:
            if script_pk is None or case.script_id != first_script_id:
                script_pk = self._s.execute(
                    select(TestScriptORM.id).where(
                        TestScriptORM.script_id == case.script_id
                    )
                ).scalar_one_or_none()
            if script_pk is None:
                raise ValueError(f"测试脚本不存在: {case.script_id}")
            orm = ScriptCaseORM(
                case_id=case.case_id,
                script_pk=script_pk,
                stable_key=case.stable_key,
                name=case.name,
                parent_path=case.parent_path,
                tags=case.tags,
                params=case.params,
                avg_duration_s=case.avg_duration_s,
                duration_samples=case.duration_samples,
                order_index=case.order_index,
                deleted=case.deleted,
            )
            self._s.add(orm)
        self._s.flush()
        # flush 后按 stable_key 回读已落库用例（保持与入参顺序一致）
        return [
            self.get_by_stable_key(case.script_id, case.stable_key) or case
            for case in cases
        ]

    def update(self, case: ScriptCase) -> ScriptCase:
        orm = self._s.get(ScriptCaseORM, case.id)
        if orm is None:
            raise ValueError(f"脚本用例不存在: id={case.id}")
        orm.stable_key = case.stable_key
        orm.name = case.name
        orm.parent_path = case.parent_path
        orm.tags = case.tags
        orm.params = case.params
        orm.avg_duration_s = case.avg_duration_s
        orm.duration_samples = case.duration_samples
        orm.order_index = case.order_index
        orm.deleted = case.deleted
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)
