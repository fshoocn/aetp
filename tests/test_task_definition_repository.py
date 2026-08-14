"""P3.3：test_tasks 任务定义仓储测试。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from aetp_protocol.capabilities import HardwareRequirements

from master.domain.enums import (
    AccountStatus,
    PlatformRole,
    ProjectStatus,
    ScriptParseLocation,
    ScriptParseStatus,
)
from master.domain.models import Project, TestScript, TestTask, User
from master.domain.time import utcnow


def _uow(container):
    """container.uow_factory() 返回工厂单例；再调用一次得到可 with 的 UoW。"""
    return container.uow_factory()()


def _seed(container) -> tuple[int, str]:
    """创建用户+项目+已解析脚本，返回 (user.id, script_id)。"""
    with _uow(container) as uow:
        user = uow.users.add(
            User(
                id=None,
                username="task_owner",
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
    return user.id, script.script_id


def _make_task(created_by: int, script_id: str, name: str = "reg", **kw) -> TestTask:
    task = TestTask(
        project_id="p1",
        task_id=f"T-{name}",
        script_id=script_id,
        script_version=1,
        task_type="pytest",
        name=name,
        default_case_selection=["can_open_channel", "can_send_frame"],
        node_ids=["bench-001", "bench-002"],
        split_policy={"type": "by_time", "target_duration_s": 300},
        retry_policy={"max_attempts": 2, "failover_nodes": True, "case_retry": 1},
        timeout_s=1800,
        enabled=True,
        priority=0,
        created_by=created_by,
    )
    for key, value in kw.items():
        setattr(task, key, value)
    return task


def test_add_and_get_by_task_id(client):
    container = client.app.state.container
    user_id, script_id = _seed(container)
    with _uow(container) as uow:
        created = uow.test_tasks.add(_make_task(user_id, script_id))
        assert created.id is not None
        fetched = uow.test_tasks.get_by_task_id(created.task_id, "p1")
        assert fetched is not None
        assert fetched.project_id == "p1"
        assert fetched.script_id == script_id
        assert fetched.script_version == 1
        assert fetched.task_type == "pytest"
        assert fetched.default_case_selection == ["can_open_channel", "can_send_frame"]
        assert fetched.node_ids == ["bench-001", "bench-002"]
        assert fetched.split_policy == {"type": "by_time", "target_duration_s": 300}
        assert fetched.retry_policy == {
            "max_attempts": 2,
            "failover_nodes": True,
            "case_retry": 1,
        }
        assert fetched.timeout_s == 1800
        assert fetched.enabled is True
        assert fetched.created_by == user_id


def test_get_by_task_id_cross_project_not_found(client):
    """task_id 全局唯一但按 project_id 限定时，跨项目查不到。"""
    container = client.app.state.container
    user_id, script_id = _seed(container)
    with _uow(container) as uow:
        created = uow.test_tasks.add(_make_task(user_id, script_id))
        assert uow.test_tasks.get_by_task_id(created.task_id, "other-proj") is None
        assert uow.test_tasks.get_by_task_id(created.task_id) is not None


def test_duplicate_name_in_project_raises(client):
    container = client.app.state.container
    user_id, script_id = _seed(container)
    with pytest.raises(IntegrityError):
        with _uow(container) as uow:
            uow.test_tasks.add(_make_task(user_id, script_id, name="reg"))
            uow.test_tasks.add(_make_task(user_id, script_id, name="reg"))


def test_find_by_name(client):
    container = client.app.state.container
    user_id, script_id = _seed(container)
    with _uow(container) as uow:
        uow.test_tasks.add(_make_task(user_id, script_id, name="nightly"))
        found = uow.test_tasks.find_by_name("p1", "nightly")
        assert found is not None and found.name == "nightly"
        assert uow.test_tasks.find_by_name("p1", "missing") is None


def test_list_by_project_pagination_and_enabled_filter(client):
    container = client.app.state.container
    user_id, script_id = _seed(container)
    with _uow(container) as uow:
        for i in range(3):
            uow.test_tasks.add(_make_task(user_id, script_id, name=f"t{i}"))
        uow.test_tasks.add(_make_task(user_id, script_id, name="disabled", enabled=False))

    with _uow(container) as uow:
        all_tasks = uow.test_tasks.list_by_project("p1")
        assert len(all_tasks) == 4
        only_enabled = uow.test_tasks.list_by_project("p1", enabled=True)
        assert len(only_enabled) == 3
        page1 = uow.test_tasks.list_by_project("p1", limit=2, offset=0)
        page2 = uow.test_tasks.list_by_project("p1", limit=2, offset=2)
        assert len(page1) == 2 and len(page2) == 2


def test_add_missing_script_version_raises(client):
    container = client.app.state.container
    user_id, script_id = _seed(container)
    with _uow(container) as uow:
        with pytest.raises(ValueError, match="脚本版本不存在"):
            uow.test_tasks.add(_make_task(user_id, script_id, name="bad", script_version=99))


def test_update_switches_script_version(client):
    """script_ref 切换：PATCH 到脚本新版本（§18.4 版本升级）。

    注意 test_scripts.script_id 全局唯一（每个版本独立 script_id），
    故 v2 需用新的 script_id（业务键仍为同 name 下 version 递增）。
    """
    container = client.app.state.container
    user_id, script_id = _seed(container)
    new_script_id = "S-reg-2"
    with _uow(container) as uow:
        uow.test_scripts.add(
            TestScript(
                id=None,
                project_id="p1",
                script_id=new_script_id,
                task_type="pytest",
                name="reg",
                version=2,
                file_ref=f"data/scripts/{new_script_id}/2/",
                size=2048,
                sha256="b" * 64,
                config={},
                hardware_requirements=HardwareRequirements(),
                parse_status=ScriptParseStatus.PARSED,
                parse_location=ScriptParseLocation.MASTER,
                result_parse_location=ScriptParseLocation.MASTER,
                plugin_version="1.0.0",
                created_by=user_id,
            )
        )
    with _uow(container) as uow:
        task = uow.test_tasks.add(_make_task(user_id, script_id, name="upgrade"))
        task.script_id = new_script_id
        task.script_version = 2
        task.enabled = False
        task.node_ids = ["bench-001"]
        updated = uow.test_tasks.update(task)
        assert updated.script_id == new_script_id
        assert updated.script_version == 2
        assert updated.enabled is False
        assert updated.node_ids == ["bench-001"]


def test_update_script_ref_missing_raises(client):
    container = client.app.state.container
    user_id, script_id = _seed(container)
    with _uow(container) as uow:
        task = uow.test_tasks.add(_make_task(user_id, script_id, name="switch"))
        task.script_version = 99
        with pytest.raises(ValueError, match="脚本版本不存在"):
            uow.test_tasks.update(task)
