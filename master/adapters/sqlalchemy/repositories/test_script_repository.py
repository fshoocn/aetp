"""SQLAlchemy 测试脚本仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from aetp_protocol.capabilities import HardwareRequirements

from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.adapters.sqlalchemy.orm import TestScript as TestScriptORM
from master.domain.enums import ScriptParseLocation, ScriptParseStatus
from master.domain.models import TestScript
from master.domain.repositories import TestScriptRepository


def _to_domain(orm: TestScriptORM) -> TestScript:
    return TestScript(
        id=orm.id,
        project_id=orm.project.project_id if orm.project is not None else "",
        script_id=orm.script_id,
        task_type=orm.task_type,
        name=orm.name,
        version=orm.version,
        file_ref=orm.file_ref,
        size=orm.size,
        sha256=orm.sha256,
        config=dict(orm.config or {}),
        hardware_requirements=HardwareRequirements.model_validate(
            orm.hardware_requirements or {}
        ),
        parse_status=ScriptParseStatus(orm.parse_status),
        parse_location=ScriptParseLocation(orm.parse_location),
        result_parse_location=ScriptParseLocation(orm.result_parse_location),
        plugin_version=orm.plugin_version,
        created_by=orm.created_by,
        last_parsed_at=orm.last_parsed_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class TestScriptRepositoryImpl(TestScriptRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_script_id(self, script_id: str) -> TestScript | None:
        orm = self._s.execute(
            select(TestScriptORM)
            .options(joinedload(TestScriptORM.project))
            .where(TestScriptORM.script_id == script_id)
        ).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def get_by_hash(self, sha256: str) -> TestScript | None:
        """按内容哈希查找（同 hash 重复上传幂等复用）。"""
        orm = self._s.execute(
            select(TestScriptORM)
            .options(joinedload(TestScriptORM.project))
            .where(TestScriptORM.sha256 == sha256)
        ).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def find_by_name_version(
        self, project_id: str, name: str, version: int
    ) -> TestScript | None:
        orm = self._s.execute(
            select(TestScriptORM)
            .options(joinedload(TestScriptORM.project))
            .where(
                TestScriptORM.project_pk
                == select(ProjectORM.id)
                .where(ProjectORM.project_id == project_id)
                .scalar_subquery(),
                TestScriptORM.name == name,
                TestScriptORM.version == version,
            )
        ).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list_by_project(
        self, project_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[TestScript]:
        stmt = (
            select(TestScriptORM)
            .options(joinedload(TestScriptORM.project))
            .where(
                TestScriptORM.project_pk
                == select(ProjectORM.id)
                .where(ProjectORM.project_id == project_id)
                .scalar_subquery()
            )
            .order_by(TestScriptORM.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def add(self, script: TestScript) -> TestScript:
        project_pk = self._s.execute(
            select(ProjectORM.id).where(ProjectORM.project_id == script.project_id)
        ).scalar_one_or_none()
        if project_pk is None:
            raise ValueError(f"项目不存在: {script.project_id}")
        if script.created_by is None:
            raise ValueError("缺少上传者 created_by")
        orm = TestScriptORM(
            script_id=script.script_id,
            project_pk=project_pk,
            task_type=script.task_type,
            name=script.name,
            version=script.version,
            file_ref=script.file_ref,
            size=script.size,
            sha256=script.sha256,
            config=script.config,
            hardware_requirements=script.hardware_requirements.model_dump(
                mode="json", exclude_none=True
            ),
            parse_status=script.parse_status.value,
            parse_location=script.parse_location.value,
            result_parse_location=script.result_parse_location.value,
            plugin_version=script.plugin_version,
            created_by=script.created_by,
            last_parsed_at=script.last_parsed_at,
        )
        self._s.add(orm)
        self._s.flush()
        return _to_domain(orm)

    def update(self, script: TestScript) -> TestScript:
        orm = self._s.get(TestScriptORM, script.id)
        if orm is None:
            raise ValueError(f"测试脚本不存在: id={script.id}")
        orm.file_ref = script.file_ref
        orm.size = script.size
        orm.sha256 = script.sha256
        orm.config = script.config
        orm.hardware_requirements = script.hardware_requirements.model_dump(
            mode="json", exclude_none=True
        )
        orm.parse_status = script.parse_status.value
        orm.parse_location = script.parse_location.value
        orm.result_parse_location = script.result_parse_location.value
        orm.plugin_version = script.plugin_version
        orm.last_parsed_at = script.last_parsed_at
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)
