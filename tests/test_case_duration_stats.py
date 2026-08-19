from __future__ import annotations

from aetp_protocol.capabilities import HardwareRequirements

from master.application.services.case_duration_service import (
    CaseDurationStatsService,
)
from master.domain.enums import (
    AccountStatus,
    PlatformRole,
    ProjectStatus,
    ScriptParseLocation,
    ScriptParseStatus,
)
from master.domain.models import Project, ScriptCase, TestScript, User
from master.domain.time import utcnow


def _uow(container):
    return container.uow_factory()()


def _seed(container):
    with _uow(container) as uow:
        user = uow.users.add(
            User(
                id=None,
                username="duration_owner",
                password_hash="h",
                display_name="",
                account_status=AccountStatus.ACTIVE,
                platform_role=PlatformRole.USER,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        uow.projects.add(
            Project(
                id=None,
                project_id="p-duration",
                project_key="P-DURATION",
                name="Duration",
                description="",
                status=ProjectStatus.ACTIVE,
                created_by=user.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        script = uow.test_scripts.add(
            TestScript(
                project_id="p-duration",
                script_id="S-duration",
                task_type="pytest",
                name="duration",
                version=1,
                file_ref="data/scripts/S-duration/1.py",
                size=1,
                sha256="a" * 64,
                config={},
                hardware_requirements=HardwareRequirements(),
                parse_status=ScriptParseStatus.PARSED,
                parse_location=ScriptParseLocation.MASTER,
                result_parse_location=ScriptParseLocation.MASTER,
                plugin_version="1.0.0",
                created_by=user.id,
            )
        )
        uow.script_cases.add(
            ScriptCase(
                script_id=script.script_id,
                case_id="C-duration",
                stable_key="test_duration.py::test_case",
                name="test_case",
            )
        )


def _record(service, container, **kwargs):
    with _uow(container) as uow:
        return service.record_success(
            uow,
            script_id="S-duration",
            project_id="p-duration",
            run_id="R-duration",
            shard_id="SH-duration",
            attempt_no=1,
            case_key="test_duration.py::test_case",
            **kwargs,
        )


def test_success_duration_uses_rolling_average(client) -> None:
    container = client.app.state.container
    _seed(container)
    service = CaseDurationStatsService(
        default_duration_s=60.0, anomaly_percent=300.0
    )

    first = _record(service, container, duration_ms=10_000)
    second = _record(service, container, duration_ms=20_000)

    assert first.accepted is True
    assert first.average_duration_s == 10.0
    assert first.duration_samples == 1
    assert second.average_duration_s == 15.0
    assert second.duration_samples == 2

    with _uow(container) as uow:
        case = uow.script_cases.get_by_stable_key(
            "S-duration", "test_duration.py::test_case"
        )
        assert case is not None
        assert case.avg_duration_s == 15.0
        assert case.duration_samples == 2


def test_missing_duration_is_not_counted(client) -> None:
    container = client.app.state.container
    _seed(container)
    service = CaseDurationStatsService()

    result = _record(service, container, duration_ms=None)

    assert result.accepted is False
    assert result.reason == "missing_duration"
    with _uow(container) as uow:
        case = uow.script_cases.get_by_stable_key(
            "S-duration", "test_duration.py::test_case"
        )
        assert case is not None
        assert case.avg_duration_s is None
        assert case.duration_samples == 0


def test_anomalous_duration_is_discarded_and_notified(client) -> None:
    container = client.app.state.container
    _seed(container)
    service = CaseDurationStatsService(
        default_duration_s=60.0, anomaly_percent=50.0
    )
    _record(service, container, duration_ms=10_000)

    result = _record(service, container, duration_ms=20_000)

    assert result.accepted is False
    assert result.reason == "deviation_exceeded"
    with _uow(container) as uow:
        case = uow.script_cases.get_by_stable_key(
            "S-duration", "test_duration.py::test_case"
        )
        assert case is not None
        assert case.avg_duration_s == 10.0
        assert case.duration_samples == 1
        events = uow.domain_events.list(project_id="p-duration")
        assert len(events) == 1
        assert events[0].event_type == "case.duration_anomaly"
        assert events[0].payload["duration_s"] == 20.0
        assert events[0].payload["threshold_percent"] == 50.0