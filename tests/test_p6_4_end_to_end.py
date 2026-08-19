"""P6.4：随包执行插件端到端闭环测试。

覆盖 Master 触发 → 分片/调度 → 投影（ack/result/log）与 Agent 编排器
（执行 → 日志 flush → result 上报）两端，以及 HTTP 触发端点。

验收要点（§15.3 P6.4：HTTP 创建任务到最终结果可见）：
1. 触发服务：创建 Run + Shards 并派发（run.assign outbox / attempt dispatching）
2. 投影服务：ack 推进 attempt/runs；result 落终态 + Run 汇总 + 设备释放
3. 投影服务：日志批按 (run_id, sequence) 幂等落库
4. Agent 编排器：执行插件后产生 run.result 与 run.log 到 outbox
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from aetp_protocol.capabilities import HardwareRequirements
from aetp_protocol.logs import LogLevel, RunLogBatch, RunLogEntry
from aetp_protocol.payloads import (
    CaseResultEntry,
    RunAckPayload,
    RunResultPayload,
)
from aetp_protocol.plugin import CaseInfo, PluginMetadata, PluginPackage, ShardSpec

from master.domain.enums import (
    AccountStatus,
    DeviceStatus,
    NodeStatus,
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
    Device,
    Node,
    Project,
    ProjectNodeBinding,
    ScriptCase,
    TestScript,
    TestTask,
    User,
)
from master.domain.time import utcnow
from master.plugins import PluginRegistry


def _uow(container):
    return container.uow_factory()()


# ---------------------------------------------------------------------------
# 测试替身插件（Master 面 + Agent 面）
# ---------------------------------------------------------------------------


class _SplitPlugin:
    """Master 面：无硬件需求，split 按 cases_per_shard 切分。"""

    task_type = "e2e_test"
    display_name = "E2E Test"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    config_schema = {"type": "object"}
    upload_spec = {"extensions": [".py"]}

    def verify_script(self, script_dir, config):
        return []

    async def parse_cases(self, script_dir, config):
        return [CaseInfo(stable_key=f"c{i}", name=f"C{i}") for i in range(4)]

    async def split_shards(self, cases, policy, config):
        per = int(policy.get("cases_per_shard", 2))
        return [
            ShardSpec(
                case_keys=tuple(c.stable_key for c in cases[i : i + per]),
                execution_params={"shard": i // per},
            )
            for i in range(0, len(cases), per)
        ]

    def build_task_definition(self, config, cases):
        return object()

    def result_schema(self, config):
        return {"type": "object"}

    def hardware_requirements(self, config, cases):
        return HardwareRequirements()


def _register_plugin(container) -> None:
    registry = container.plugin_registry()
    if registry.get("e2e_test") is not None:
        return
    registry.register(
        PluginPackage(
            metadata=PluginMetadata(
                task_type="e2e_test",
                plugin_version="1.0.0",
                supported_versions=frozenset({"1.0.0"}),
            ),
            master=_SplitPlugin(),
            agent=object(),
        )
    )


def _seed(container, *, node_id: str = "node-a") -> tuple[int, str]:
    """创建用户+项目+节点+脚本+用例+任务定义+绑定。"""
    _register_plugin(container)
    with _uow(container) as uow:
        user = uow.users.add(
            User(
                id=None,
                username="e2e_owner",
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
                script_id="S-e2e",
                task_type="e2e_test",
                name="e2e",
                version=1,
                file_ref="data/scripts/S-e2e/1",
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
        for i in range(4):
            uow.script_cases.add(
                ScriptCase(
                    script_id=script.script_id,
                    case_id=f"CASE-{i}",
                    stable_key=f"c{i}",
                    name=f"C{i}",
                    order_index=i,
                )
            )
        node = uow.nodes.save(
            Node(
                id=None,
                node_id=node_id,
                name=node_id,
                hostname=node_id,
                status=NodeStatus.ONLINE,
                online=True,
                enabled=True,
                last_seen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        uow.devices.add(
            Device(
                id=None,
                device_id=f"{node_id}-device",
                node_id=node_id,
                name="d",
                status=DeviceStatus.ONLINE,
                online=True,
            )
        )
        uow.bindings.add(
            ProjectNodeBinding(
                id=None,
                project_id="p1",
                node_id=node_id,
                enabled=True,
                assigned_by=user.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        task = uow.test_tasks.add(
            TestTask(
                project_id="p1",
                task_id="T-e2e",
                script_id=script.script_id,
                script_version=1,
                task_type="e2e_test",
                name="e2e-task",
                default_case_selection=[f"c{i}" for i in range(4)],
                node_ids=[node_id],
                split_policy={"type": "by_case_count", "cases_per_shard": 2},
                retry_policy={},
                timeout_s=120,
                enabled=True,
                created_by=user.id,
            )
        )
    return user.id, task.task_id


# ---------------------------------------------------------------------------
# 触发服务
# ---------------------------------------------------------------------------


def test_trigger_creates_run_shards_and_schedules(client) -> None:
    container = client.app.state.container
    user_id, task_id = _seed(container)

    result = asyncio.run(
        container.run_trigger_service().trigger(
            task_id,
            project_id="p1",
            triggered_by_user_id=user_id,
            trigger_type=TriggerType.MANUAL_WEB,
        )
    )

    assert result.task_id == task_id
    assert len(result.shard_ids) == 2  # 4 cases / 2 per shard
    assert result.scheduled == 2

    with _uow(container) as uow:
        run = uow.task_runs.get_by_run_id(result.run_id)
        assert run is not None
        assert run.status is RunStatus.DISPATCHED
        shards = uow.run_shards.list_by_run(result.run_id)
        assert [s.case_keys for s in shards] == [["c0", "c1"], ["c2", "c3"]]
        # 每个 shard 都有一个 dispatching attempt + run.assign outbox
        for shard in shards:
            attempts = uow.shard_attempts.list_by_shard(shard.shard_id)
            assert len(attempts) == 1
            assert attempts[0].status is ShardAttemptStatus.DISPATCHED


def test_trigger_raises_task_not_found(client) -> None:
    container = client.app.state.container
    from master.application.errors import TaskNotFoundError

    with pytest.raises(TaskNotFoundError):
        asyncio.run(
            container.run_trigger_service().trigger(
                "missing", project_id="p1", triggered_by_user_id=None
            )
        )


# ---------------------------------------------------------------------------
# 投影服务
# ---------------------------------------------------------------------------


def _run_id_after_trigger(client) -> str:
    container = client.app.state.container
    user_id, task_id = _seed(container)
    return asyncio.run(
        container.run_trigger_service().trigger(
            task_id, project_id="p1", triggered_by_user_id=user_id
        )
    ).run_id


def test_projection_ack_advances_attempt_and_run(client) -> None:
    container = client.app.state.container
    run_id = _run_id_after_trigger(client)

    with _uow(container) as uow:
        shard = uow.run_shards.list_by_run(run_id)[0]
        attempt = uow.shard_attempts.list_by_shard(shard.shard_id)[0]
        dispatch_id = attempt.attempt_id
        attempt_no = attempt.attempt_no

    service = container.run_projection_service()
    pr = service.handle_ack(
        "node-a",
        RunAckPayload(
            run_id=run_id,
            attempt_no=attempt_no,
            dispatch_id=dispatch_id,
            accepted=True,
            reason="ok",
        ),
    )
    assert pr.handled is True

    with _uow(container) as uow:
        attempt = uow.shard_attempts.get_by_attempt_id(dispatch_id)
        assert attempt.status is ShardAttemptStatus.ACKED
        run = uow.task_runs.get_by_run_id(run_id)
        assert run.status is RunStatus.ACKED


def test_projection_result_finalizes_run_and_releases_devices(client) -> None:
    container = client.app.state.container
    run_id = _run_id_after_trigger(client)

    with _uow(container) as uow:
        shard = uow.run_shards.list_by_run(run_id)[0]
        attempt = uow.shard_attempts.list_by_shard(shard.shard_id)[0]
        dispatch_id = attempt.attempt_id
        attempt_no = attempt.attempt_no

    service = container.run_projection_service()
    # ack 先推进到 acked
    service.handle_ack(
        "node-a",
        RunAckPayload(
            run_id=run_id,
            attempt_no=attempt_no,
            dispatch_id=dispatch_id,
            accepted=True,
        ),
    )
    pr = service.handle_result(
        "node-a",
        RunResultPayload(
            run_id=run_id,
            shard_id=shard.shard_id,
            attempt_no=attempt_no,
            status="succeeded",
            passed=True,
            case_results=[
                CaseResultEntry(case_key="c0", status="passed", duration_ms=12),
                CaseResultEntry(case_key="c1", status="passed", duration_ms=15),
            ],
        ),
    )
    assert pr.handled is True

    with _uow(container) as uow:
        attempt = uow.shard_attempts.get_by_attempt_id(dispatch_id)
        assert attempt.status is ShardAttemptStatus.SUCCEEDED
        shard = uow.run_shards.get_by_shard_id(shard.shard_id)
        assert shard.status is ShardStatus.SUCCEEDED
        # case 结果已落库
        cases = uow.run_case_results.list_by_shard(run_id, shard.shard_id)
        assert {c.case_key for c in cases} == {"c0", "c1"}
        # P6.8：成功 case 耗时回写脚本用例统计
        c0 = uow.script_cases.get_by_stable_key("S-e2e", "c0")
        c1 = uow.script_cases.get_by_stable_key("S-e2e", "c1")
        assert c0 is not None and c0.avg_duration_s == 0.012
        assert c0.duration_samples == 1
        assert c1 is not None and c1.avg_duration_s == 0.015
        assert c1.duration_samples == 1


def test_projection_result_idempotent_single_final_result(client) -> None:
    """D-19：一个 attempt 只接收一个最终结果。"""
    container = client.app.state.container
    run_id = _run_id_after_trigger(client)

    with _uow(container) as uow:
        shard = uow.run_shards.list_by_run(run_id)[0]
        attempt = uow.shard_attempts.list_by_shard(shard.shard_id)[0]

    service = container.run_projection_service()
    service.handle_ack(
        "node-a",
        RunAckPayload(
            run_id=run_id,
            attempt_no=attempt.attempt_no,
            dispatch_id=attempt.attempt_id,
            accepted=True,
        ),
    )
    first = service.handle_result(
        "node-a",
        RunResultPayload(
            run_id=run_id,
            shard_id=shard.shard_id,
            attempt_no=attempt.attempt_no,
            status="succeeded",
            passed=True,
        ),
    )
    second = service.handle_result(
        "node-a",
        RunResultPayload(
            run_id=run_id,
            shard_id=shard.shard_id,
            attempt_no=attempt.attempt_no,
            status="failed",
            passed=False,
        ),
    )
    assert first.handled is True
    assert second.handled is False

    with _uow(container) as uow:
        attempt = uow.shard_attempts.get_by_attempt_id(attempt.attempt_id)
        assert attempt.status is ShardAttemptStatus.SUCCEEDED
        result = uow.run_results.get_by_run_id(run_id)
        assert result.status is RunStatus.SUCCEEDED
        assert result.passed is True


def test_projection_log_batch_idempotent_by_sequence(client) -> None:
    container = client.app.state.container
    run_id = _run_id_after_trigger(client)

    batch = RunLogBatch(
        run_id=run_id,
        first_sequence=1,
        entries=[
            RunLogEntry(
                project_id="p1",
                task_id="T-e2e",
                run_id=run_id,
                node_id="node-a",
                sequence=1,
                level=LogLevel.INFO,
                message="hello",
                occurred_at=datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc),
            ),
            RunLogEntry(
                project_id="p1",
                task_id="T-e2e",
                run_id=run_id,
                node_id="node-a",
                sequence=2,
                level=LogLevel.ERROR,
                message="boom",
                occurred_at=datetime(2026, 8, 17, 12, 0, 1, tzinfo=timezone.utc),
            ),
        ],
    )

    service = container.run_projection_service()
    first = service.handle_log("node-a", batch)
    second = service.handle_log("node-a", batch)
    assert first.handled is True
    assert second.handled is False

    with _uow(container) as uow:
        logs = uow.run_logs.list_by_run(run_id)
        assert [l.sequence for l in logs] == [1, 2]
        assert uow.run_logs.get_max_sequence(run_id) == 2


# ---------------------------------------------------------------------------
# HTTP 触发端点
# ---------------------------------------------------------------------------


def test_http_trigger_run_endpoint(client, auth_header) -> None:
    container = client.app.state.container
    _user_id, task_id = _seed(container)
    _add_tester_as_member(container)

    resp = client.post(
        "/api/v1/projects/p1/runs",
        json={"task_id": task_id},
        headers=auth_header,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["run_id"].startswith("R-")
    assert data["status"] == "created"

    # 查询详情可见 shards
    detail = client.get(
        f"/api/v1/projects/p1/runs/{data['run_id']}",
        headers=auth_header,
    )
    assert detail.status_code == 200
    assert len(detail.json()["shards"]) == 2


def test_http_list_runs(client, auth_header) -> None:
    container = client.app.state.container
    _user_id, task_id = _seed(container)
    _add_tester_as_member(container)
    asyncio.run(
        container.run_trigger_service().trigger(
            task_id, project_id="p1", triggered_by_user_id=None
        )
    )

    resp = client.get("/api/v1/projects/p1/runs", headers=auth_header)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def _add_tester_as_member(container) -> None:
    """将 conftest 的 tester 用户加入 p1 作为 operator。"""
    with container.database().session_scope() as s:
        from sqlalchemy import text as sa_text

        tester_id = s.execute(
            sa_text("SELECT id FROM users WHERE username='tester'")
        ).scalar_one()
        project_pk = s.execute(
            sa_text("SELECT id FROM projects WHERE project_id='p1'")
        ).scalar_one()
        s.execute(
            sa_text(
                "INSERT OR IGNORE INTO project_members "
                "(project_pk, user_id, project_role, assigned_by, created_at, updated_at) "
                "VALUES (:ppk, :uid, 'operator', :uid, :now, :now)"
            ),
            {
                "ppk": project_pk,
                "uid": tester_id,
                "now": datetime.now(timezone.utc).replace(tzinfo=None),
            },
        )

