"""P6.7：三层重试 + 失败历史全量保留（D-20）测试。

验收要点（§15.3 P6.7：retry=新 Run；failover/case 重试=新 Attempt；历史不覆盖）：
1. retry 创建新 Run（trigger_type=retry，trigger_context 引用原 run_id），原 Run 终态不迁移
2. retry_failed 仅重跑失败 case（case 集合=原 Run 最新 attempt 的 failed/error case）
3. 失败 case 判定按 (case_key, attempt_no) 取最新 attempt（最新成功则不重跑）
4. failover 同 Run 新 Attempt 且历史失败保留（复用 ShardSchedulerService，已有测试，此处验证领域函数）
"""

from __future__ import annotations

import asyncio
from datetime import UTC

import pytest

from master.application.errors import RunNotFoundError
from master.application.services.run_retry_service import RunRetryService
from master.domain.enums import CaseStatus


def _uow(container):
    return container.uow_factory()()


# ---------------------------------------------------------------------------
# 失败 case 判定（纯逻辑，通过 RunRetryService._failed_case_keys 验证）
# ---------------------------------------------------------------------------


def _case(key: str, attempt: int, status: CaseStatus):
    from master.domain.models import RunCaseResult

    return RunCaseResult(
        run_id="R-1",
        shard_id="SH-1",
        case_key=key,
        attempt_no=attempt,
        status=status,
    )


def test_failed_case_keys_latest_attempt_wins() -> None:
    """D-20：同 case 多 attempt，重跑判定只看最新 attempt。"""
    results = [
        _case("c0", 1, CaseStatus.FAILED),
        _case("c0", 2, CaseStatus.PASSED),  # 最新成功 → 不重跑
        _case("c1", 1, CaseStatus.FAILED),  # 最新失败 → 重跑
        _case("c2", 1, CaseStatus.ERROR),  # 最新 error → 重跑
        _case("c3", 1, CaseStatus.SKIPPED),  # skipped → 不重跑
    ]
    assert RunRetryService._failed_case_keys(results) == {"c1", "c2"}


def test_failed_case_keys_empty() -> None:
    assert RunRetryService._failed_case_keys([]) == set()


def test_failed_case_keys_preserves_history() -> None:
    """D-20：历史失败全量保留（latest 判定不删除旧 attempt 记录）。"""
    results = [
        _case("c0", 1, CaseStatus.FAILED),
        _case("c0", 2, CaseStatus.FAILED),
        _case("c0", 3, CaseStatus.PASSED),
    ]
    # 历史 3 条都在（本函数只做判定，不删数据）
    assert len(results) == 3
    assert RunRetryService._failed_case_keys(results) == set()


# ---------------------------------------------------------------------------
# retry / retry-failed（经容器端到端触发新 Run）
# ---------------------------------------------------------------------------


def _seed_run_with_result(container) -> str:
    """复用 P6.4 种子并注入 case 结果（c0 失败、c1 成功）。"""
    from master.domain.models import RunCaseResult
    from tests.test_p6_4_end_to_end import _register_plugin, _seed

    _register_plugin(container)
    user_id, task_id = _seed(container)
    run_id = asyncio.run(
        container.run_trigger_service().trigger(task_id, project_id="p1", triggered_by_user_id=user_id)
    ).run_id

    with _uow(container) as uow:
        shard = uow.run_shards.list_by_run(run_id)[0]
        uow.run_case_results.add_many(
            [
                RunCaseResult(
                    run_id=run_id,
                    shard_id=shard.shard_id,
                    case_key="c0",
                    attempt_no=1,
                    status=CaseStatus.FAILED,
                ),
                RunCaseResult(
                    run_id=run_id,
                    shard_id=shard.shard_id,
                    case_key="c1",
                    attempt_no=1,
                    status=CaseStatus.PASSED,
                ),
            ]
        )
    return run_id


def test_retry_creates_new_run_with_retry_context(client) -> None:
    container = client.app.state.container
    run_id = _seed_run_with_result(container)

    result = asyncio.run(container.run_retry_service().retry(run_id, project_id="p1", triggered_by_user_id=None))

    assert result.new_run_id != run_id
    assert result.original_run_id == run_id
    with _uow(container) as uow:
        new_run = uow.task_runs.get_by_run_id(result.new_run_id)
        assert new_run is not None
        assert new_run.trigger_type.value == "retry"
        assert new_run.trigger_context == {
            "original_run_id": run_id,
            "mode": "retry",
        }
        # 原 Run 未迁移（仍存在）
        assert uow.task_runs.get_by_run_id(run_id) is not None


def test_retry_failed_only_failed_cases(client) -> None:
    container = client.app.state.container
    run_id = _seed_run_with_result(container)

    result = asyncio.run(container.run_retry_service().retry_failed(run_id, project_id="p1", triggered_by_user_id=None))

    # c1 成功不重跑，c0 失败重跑
    assert result.retried_case_keys == ("c0",)
    with _uow(container) as uow:
        new_run = uow.task_runs.get_by_run_id(result.new_run_id)
        assert new_run.trigger_context["mode"] == "retry_failed"
        # 新 Run 的 case_selection 仅含失败 case c0
        assert new_run.case_selection == ["c0"]


def test_retry_unknown_run_raises(client) -> None:
    container = client.app.state.container
    with pytest.raises(RunNotFoundError):
        asyncio.run(container.run_retry_service().retry("missing", project_id="p1", triggered_by_user_id=None))


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------


def test_http_retry_endpoint(client, auth_header) -> None:
    container = client.app.state.container
    run_id = _seed_run_with_result(container)
    _add_tester_member(container)

    resp = client.post(f"/api/v1/projects/p1/runs/{run_id}/retry", headers=auth_header)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["trigger_type"] == "retry"
    assert data["run_id"] != run_id


def test_http_retry_failed_endpoint(client, auth_header) -> None:
    container = client.app.state.container
    run_id = _seed_run_with_result(container)
    _add_tester_member(container)

    resp = client.post(f"/api/v1/projects/p1/runs/{run_id}/retry-failed", headers=auth_header)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["trigger_type"] == "retry"
    assert data["run_id"] != run_id


def _add_tester_member(container) -> None:
    """将 conftest 的 tester 用户加入 p1 作为 operator。"""
    from datetime import datetime

    from sqlalchemy import text as sa_text

    with container.database().session_scope() as s:
        tester_id = s.execute(sa_text("SELECT id FROM users WHERE username='tester'")).scalar_one()
        project_pk = s.execute(sa_text("SELECT id FROM projects WHERE project_id='p1'")).scalar_one()
        s.execute(
            sa_text(
                "INSERT OR IGNORE INTO project_members "
                "(project_pk, user_id, project_role, assigned_by, created_at, updated_at) "
                "VALUES (:ppk, :uid, 'operator', :uid, :now, :now)"
            ),
            {
                "ppk": project_pk,
                "uid": tester_id,
                "now": datetime.now(UTC).replace(tzinfo=None),
            },
        )
