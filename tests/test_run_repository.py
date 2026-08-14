"""P3.4：Run 执行域仓储测试（task_runs/run_shards/shard_attempts/run_case_results/run_artifacts/results）。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from aetp_protocol.capabilities import HardwareRequirements

from master.domain.enums import (
    AccountStatus,
    ArtifactKind,
    CaseStatus,
    PlatformRole,
    ProjectStatus,
    RunStatus,
    ScriptParseLocation,
    ScriptParseStatus,
    ShardAttemptStatus,
    ShardStatus,
    TriggerType,
)
from master.domain.models import (
    Project,
    RunArtifact,
    RunCaseResult,
    RunResult,
    RunShard,
    ShardAttempt,
    TaskRun,
    TestScript,
    TestTask,
    User,
)
from master.domain.time import utcnow


def _uow(container):
    """container.uow_factory() 返回工厂单例；再调用一次得到可 with 的 UoW。"""
    return container.uow_factory()()


def _seed(container) -> tuple[int, str]:
    """创建用户+项目+脚本+任务定义，返回 (user.id, task_id)。"""
    with _uow(container) as uow:
        user = uow.users.add(
            User(
                id=None,
                username="run_owner",
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
                project_id="p1",
                project_key="P1",
                name="P",
                description="",
                status=ProjectStatus.ACTIVE,
                created_by=user.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
    with _uow(container) as uow:
        script = uow.test_scripts.add(
            TestScript(
                id=None,
                project_id="p1",
                script_id="S-reg-1",
                task_type="pytest",
                name="reg",
                version=1,
                file_ref="data/scripts/S-reg-1/",
                size=1024,
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
        task = uow.test_tasks.add(
            TestTask(
                id=None,
                project_id="p1",
                task_id="T-reg",
                script_id=script.script_id,
                script_version=1,
                task_type="pytest",
                name="reg",
                default_case_selection=["c1", "c2"],
                node_ids=["bench-001"],
                split_policy={"type": "by_time", "target_duration_s": 300},
                retry_policy={"max_attempts": 2, "failover_nodes": True, "case_retry": 1},
                timeout_s=1800,
                enabled=True,
                priority=0,
                created_by=user.id,
            )
        )
    return user.id, task.task_id


def _make_run(user_id: int, task_id: str, run_id: str = "R-1", **kw) -> TaskRun:
    run = TaskRun(
        run_id=run_id,
        project_id="p1",
        task_id=task_id,
        script_ref={"script_id": "S-reg-1", "version": 1, "sha256": "a" * 64},
        case_selection=["c1", "c2"],
        split_policy={"type": "by_time", "target_duration_s": 300},
        trigger_type=TriggerType.MANUAL_WEB,
        triggered_by_user_id=user_id,
        status=RunStatus.CREATED,
    )
    for key, value in kw.items():
        setattr(run, key, value)
    return run


def _make_shard(run_id: str, shard_id: str, index: int, **kw) -> RunShard:
    shard = RunShard(
        shard_id=shard_id,
        run_id=run_id,
        shard_index=index,
        case_keys=[f"c{index}"],
        execution_params={"channel": index},
        status=ShardStatus.PENDING,
    )
    for key, value in kw.items():
        setattr(shard, key, value)
    return shard


def _make_attempt(
    shard_id: str, attempt_no: int, node_id: str = "bench-001", **kw
) -> ShardAttempt:
    attempt = ShardAttempt(
        attempt_id=f"A-{shard_id}-{attempt_no}",
        shard_id=shard_id,
        attempt_no=attempt_no,
        node_id=node_id,
        status=ShardAttemptStatus.CREATED,
    )
    for key, value in kw.items():
        setattr(attempt, key, value)
    return attempt


def _make_case_result(
    run_id: str, shard_id: str, case_key: str, attempt_no: int, **kw
) -> RunCaseResult:
    result = RunCaseResult(
        run_id=run_id,
        shard_id=shard_id,
        case_key=case_key,
        attempt_no=attempt_no,
        status=CaseStatus.PASSED,
    )
    for key, value in kw.items():
        setattr(result, key, value)
    return result


def _make_artifact(run_id: str, artifact_id: str, **kw) -> RunArtifact:
    artifact = RunArtifact(
        artifact_id=artifact_id,
        run_id=run_id,
        kind=ArtifactKind.REPORT,
        file_ref=f"data/artifacts/{run_id}/report.json",
        size=100,
        sha256="c" * 64,
    )
    for key, value in kw.items():
        setattr(artifact, key, value)
    return artifact


def _make_result(run_id: str, task_id: str, **kw) -> RunResult:
    result = RunResult(
        result_id=f"RES-{run_id}",
        run_id=run_id,
        project_id="p1",
        task_id=task_id,
        passed=True,
        status=RunStatus.SUCCEEDED,
        metrics={"total": 2, "passed": 2, "duration_s": 12.5},
    )
    for key, value in kw.items():
        setattr(result, key, value)
    return result


# ---------- task_runs ----------


def test_run_add_and_get(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        created = uow.task_runs.add(_make_run(user_id, task_id))
        assert created.id is not None
        fetched = uow.task_runs.get_by_run_id("R-1", "p1")
        assert fetched is not None
        assert fetched.project_id == "p1"
        assert fetched.task_id == task_id
        assert fetched.script_ref == {
            "script_id": "S-reg-1",
            "version": 1,
            "sha256": "a" * 64,
        }
        assert fetched.case_selection == ["c1", "c2"]
        assert fetched.trigger_type == TriggerType.MANUAL_WEB
        assert fetched.triggered_by_user_id == user_id
        assert fetched.status == RunStatus.CREATED


def test_run_get_cross_project_not_found(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        uow.task_runs.add(_make_run(user_id, task_id))
        assert uow.task_runs.get_by_run_id("R-1", "other-proj") is None


def test_run_unique_run_id_raises(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with pytest.raises(IntegrityError):
        with _uow(container) as uow:
            uow.task_runs.add(_make_run(user_id, task_id, run_id="R-1"))
            uow.task_runs.add(_make_run(user_id, task_id, run_id="R-1"))


def test_run_list_filters(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        uow.task_runs.add(_make_run(user_id, task_id, run_id="R-a"))
        uow.task_runs.add(
            _make_run(
                user_id,
                task_id,
                run_id="R-b",
                trigger_type=TriggerType.SCHEDULE,
                status=RunStatus.RUNNING,
            )
        )
    with _uow(container) as uow:
        assert len(uow.task_runs.list(project_id="p1")) == 2
        assert len(uow.task_runs.list(project_id="p1", trigger_type="schedule")) == 1
        assert len(uow.task_runs.list(project_id="p1", status="running")) == 1
        assert len(uow.task_runs.list(project_id="p1", task_id=task_id)) == 2
        page = uow.task_runs.list(project_id="p1", limit=1, offset=1)
        assert len(page) == 1


def test_run_missing_task_raises(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        with pytest.raises(ValueError, match="任务定义不存在"):
            uow.task_runs.add(_make_run(user_id, "T-missing"))


def test_run_update_status(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        run = uow.task_runs.add(_make_run(user_id, task_id))
        run.status = RunStatus.RUNNING
        run.started_at = utcnow()
        updated = uow.task_runs.update(run)
        assert updated.status == RunStatus.RUNNING
        assert updated.started_at is not None


# ---------- run_shards ----------


def test_shards_add_many_and_list_order(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        uow.task_runs.add(_make_run(user_id, task_id))
        uow.run_shards.add_many(
            [
                _make_shard("R-1", "SH-1", 0),
                _make_shard("R-1", "SH-2", 1, case_keys=["c2"]),
            ]
        )
    with _uow(container) as uow:
        listed = uow.run_shards.list_by_run("R-1")
        assert [s.shard_index for s in listed] == [0, 1]
        assert listed[0].run_id == "R-1"
        assert listed[0].case_keys == ["c0"]
        assert listed[0].execution_params == {"channel": 0}
        assert listed[1].execution_params == {"channel": 1}
        assert listed[0].status == ShardStatus.PENDING


def test_shards_duplicate_index_raises(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        uow.task_runs.add(_make_run(user_id, task_id))
    with pytest.raises(IntegrityError):
        with _uow(container) as uow:
            uow.run_shards.add_many(
                [_make_shard("R-1", "SH-1", 0), _make_shard("R-1", "SH-2", 0)]
            )


# ---------- shard_attempts ----------


def test_attempts_history_preserved(client):
    """D-20：同一 Shard 多次 attempt（failover 换节点）历史全量保留。"""
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        uow.task_runs.add(_make_run(user_id, task_id))
        uow.run_shards.add(_make_shard("R-1", "SH-1", 0))
        a1 = uow.shard_attempts.add(
            _make_attempt("SH-1", 1, node_id="bench-001", status=ShardAttemptStatus.FAILED)
        )
        a2 = uow.shard_attempts.add(
            _make_attempt("SH-1", 2, node_id="bench-002", status=ShardAttemptStatus.RUNNING)
        )
        assert a1.attempt_no == 1 and a2.attempt_no == 2
    with _uow(container) as uow:
        history = uow.shard_attempts.list_by_shard("SH-1")
        assert [a.attempt_no for a in history] == [1, 2]
        assert history[0].status == ShardAttemptStatus.FAILED  # 历史失败不被覆盖
        got = uow.shard_attempts.get_by_shard_attempt("SH-1", 2)
        assert got is not None and got.node_id == "bench-002"


def test_attempts_duplicate_attempt_no_raises(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        uow.task_runs.add(_make_run(user_id, task_id))
        uow.run_shards.add(_make_shard("R-1", "SH-1", 0))
    with pytest.raises(IntegrityError):
        with _uow(container) as uow:
            uow.shard_attempts.add(_make_attempt("SH-1", 1))
            uow.shard_attempts.add(_make_attempt("SH-1", 1))


# ---------- run_case_results ----------


def test_case_results_by_attempt_preserved(client):
    """D-20：case 结果按 attempt 全量保留。"""
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        uow.task_runs.add(_make_run(user_id, task_id))
        uow.run_shards.add(_make_shard("R-1", "SH-1", 0))
        uow.run_case_results.add_many(
            [
                _make_case_result(
                    "R-1", "SH-1", "c0", 1,
                    status=CaseStatus.FAILED, error_summary="boom",
                ),
                _make_case_result(
                    "R-1", "SH-1", "c0", 2,
                    status=CaseStatus.PASSED, duration_ms=1500,
                ),
            ]
        )
    with _uow(container) as uow:
        results = uow.run_case_results.list_by_run("R-1")
        assert len(results) == 2
        assert {r.attempt_no for r in results} == {1, 2}
        failed = next(r for r in results if r.attempt_no == 1)
        assert failed.status == CaseStatus.FAILED  # 历史失败不因后续成功消失
        assert failed.error_summary == "boom"
        by_shard = uow.run_case_results.list_by_shard("R-1", "SH-1")
        assert len(by_shard) == 2
        got = uow.run_case_results.get_by_key("R-1", "SH-1", "c0", 2)
        assert got is not None and got.duration_ms == 1500


def test_case_results_duplicate_key_raises(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        uow.task_runs.add(_make_run(user_id, task_id))
        uow.run_shards.add(_make_shard("R-1", "SH-1", 0))
    with pytest.raises(IntegrityError):
        with _uow(container) as uow:
            uow.run_case_results.add_many(
                [
                    _make_case_result("R-1", "SH-1", "c0", 1),
                    _make_case_result("R-1", "SH-1", "c0", 1),
                ]
            )


# ---------- run_artifacts ----------


def test_artifacts_add_and_list_by_run(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        uow.task_runs.add(_make_run(user_id, task_id))
        uow.run_shards.add(_make_shard("R-1", "SH-1", 0))
        uow.run_artifacts.add(_make_artifact("R-1", "ART-1"))
        uow.run_artifacts.add(
            _make_artifact(
                "R-1", "ART-2",
                shard_id="SH-1", kind=ArtifactKind.LOG_ARCHIVE,
                file_ref="data/artifacts/R-1/logs.zip",
            )
        )
    with _uow(container) as uow:
        artifacts = uow.run_artifacts.list_by_run("R-1")
        assert len(artifacts) == 2
        assert {a.kind for a in artifacts} == {
            ArtifactKind.REPORT,
            ArtifactKind.LOG_ARCHIVE,
        }
        run_level = next(a for a in artifacts if a.artifact_id == "ART-1")
        assert run_level.shard_id is None  # Run 级产物 shard_id 为空
        got = uow.run_artifacts.get_by_artifact_id("ART-2")
        assert got is not None and got.shard_id == "SH-1"


# ---------- results（Run 级汇总投影） ----------


def test_run_result_add_get_and_update(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        uow.task_runs.add(_make_run(user_id, task_id))
        created = uow.run_results.add(_make_result("R-1", task_id))
        assert created.id is not None
        fetched = uow.run_results.get_by_run_id("R-1")
        assert fetched is not None
        assert fetched.project_id == "p1"
        assert fetched.task_id == task_id
        assert fetched.passed is True
        assert fetched.status == RunStatus.SUCCEEDED
        assert fetched.metrics == {"total": 2, "passed": 2, "duration_s": 12.5}
        fetched.passed = False
        fetched.status = RunStatus.FAILED
        updated = uow.run_results.update(fetched)
        assert updated.passed is False
        assert updated.status == RunStatus.FAILED


def test_run_result_unique_per_run(client):
    """results 表 run_pk 唯一：一 Run 一行汇总投影。"""
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        uow.task_runs.add(_make_run(user_id, task_id))
    with pytest.raises(IntegrityError):
        with _uow(container) as uow:
            uow.run_results.add(_make_result("R-1", task_id, result_id="RES-1"))
            uow.run_results.add(_make_result("R-1", task_id, result_id="RES-2"))


def test_run_result_missing_run_raises(client):
    container = client.app.state.container
    user_id, task_id = _seed(container)
    with _uow(container) as uow:
        with pytest.raises(ValueError, match="Run 不存在"):
            uow.run_results.add(_make_result("R-x", task_id))
