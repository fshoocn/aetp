"""P3.2：test_scripts / script_cases 仓储测试。"""

from __future__ import annotations

import pytest
from aetp_protocol.capabilities import HardwareRequirements
from sqlalchemy.exc import IntegrityError

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
    """container.uow_factory() 返回工厂单例；再调用一次得到可 with 的 UoW。"""
    return container.uow_factory()()


def _seed_user_and_project(container) -> int:
    """创建用户与项目，返回 user.id（后续脚本 created_by 使用）。"""
    with _uow(container) as uow:
        user = uow.users.add(
            User(
                id=None,
                username="uploader",
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
    return user.id


def _make_script(
    created_by: int,
    name: str = "reg",
    version: int = 1,
    sha: str = "a" * 64,
    **kw,
) -> TestScript:
    script = TestScript(
        project_id="p1",
        script_id=f"S-{name}-{version}",
        task_type="pytest",
        name=name,
        version=version,
        file_ref=f"data/scripts/S-{name}-{version}/",
        size=1024,
        sha256=sha,
        config={"channel": "can0"},
        hardware_requirements=HardwareRequirements(),
        parse_status=ScriptParseStatus.PARSED,
        parse_location=ScriptParseLocation.MASTER,
        result_parse_location=ScriptParseLocation.MASTER,
        plugin_version="1.0.0",
        created_by=created_by,
    )
    for key, value in kw.items():
        setattr(script, key, value)
    return script


def _make_case(script_id: str, stable_key: str, order_index: int = 0, **kw) -> ScriptCase:
    case = ScriptCase(
        script_id=script_id,
        case_id=f"C-{script_id}-{stable_key}",
        stable_key=stable_key,
        name=f"case {stable_key}",
        parent_path="",
        tags=["smoke"],
        params={},
        avg_duration_s=None,
        duration_samples=0,
        order_index=order_index,
        deleted=False,
    )
    for key, value in kw.items():
        setattr(case, key, value)
    return case


def test_add_and_get_by_script_id(client):
    container = client.app.state.container
    user_id = _seed_user_and_project(container)
    with _uow(container) as uow:
        created = uow.test_scripts.add(_make_script(user_id))
        assert created.id is not None
        fetched = uow.test_scripts.get_by_script_id(created.script_id)
        assert fetched is not None
        assert fetched.project_id == "p1"
        assert fetched.name == "reg"
        assert fetched.version == 1
        assert fetched.config == {"channel": "can0"}
        assert fetched.parse_status == ScriptParseStatus.PARSED


def test_get_by_hash_idempotency(client):
    """同 sha256 重复上传可幂等复用。"""
    container = client.app.state.container
    user_id = _seed_user_and_project(container)
    sha = "b" * 64
    with _uow(container) as uow:
        uow.test_scripts.add(_make_script(user_id, name="dup", version=1, sha=sha))
        hit = uow.test_scripts.get_by_hash(sha)
        assert hit is not None
        assert hit.name == "dup"
        assert uow.test_scripts.get_by_hash("0" * 64) is None


def test_duplicate_project_name_version_raises(client):
    container = client.app.state.container
    user_id = _seed_user_and_project(container)
    with pytest.raises(IntegrityError), _uow(container) as uow:
        uow.test_scripts.add(_make_script(user_id, name="reg", version=1))
        uow.test_scripts.add(_make_script(user_id, name="reg", version=1))


def test_same_name_different_version_allowed(client):
    container = client.app.state.container
    user_id = _seed_user_and_project(container)
    with _uow(container) as uow:
        v1 = uow.test_scripts.add(_make_script(user_id, name="reg", version=1))
        v2 = uow.test_scripts.add(_make_script(user_id, name="reg", version=2))
        assert v1.version == 1
        assert v2.version == 2
        found = uow.test_scripts.find_by_name_version("p1", "reg", 2)
        assert found is not None and found.script_id == v2.script_id


def test_max_version_for_name(client):
    container = client.app.state.container
    user_id = _seed_user_and_project(container)
    with _uow(container) as uow:
        uow.test_scripts.add(_make_script(user_id, name='reg', version=1))
        uow.test_scripts.add(_make_script(user_id, name='reg', version=3))
        uow.test_scripts.add(_make_script(user_id, name='other', version=5))
    with _uow(container) as uow:
        assert uow.test_scripts.max_version_for_name('p1', 'reg') == 3
        assert uow.test_scripts.max_version_for_name('p1', 'other') == 5
        assert uow.test_scripts.max_version_for_name('p1', 'nonexistent') == 0


def test_list_by_project_pagination(client):
    container = client.app.state.container
    user_id = _seed_user_and_project(container)
    with _uow(container) as uow:
        for i in range(3):
            uow.test_scripts.add(_make_script(user_id, name=f"script-{i}", version=1))
    with _uow(container) as uow:
        page1 = uow.test_scripts.list_by_project("p1", limit=2, offset=0)
        page2 = uow.test_scripts.list_by_project("p1", limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 1
        # id desc 排序
        assert page1[0].id > page1[1].id


def test_update_script_parse_status(client):
    container = client.app.state.container
    user_id = _seed_user_and_project(container)
    with _uow(container) as uow:
        script = uow.test_scripts.add(
            _make_script(
                user_id,
                parse_status=ScriptParseStatus.PENDING,
                parse_location=ScriptParseLocation.AGENT,
            )
        )
        script.parse_status = ScriptParseStatus.PARSING
        script.last_parsed_at = utcnow()
        updated = uow.test_scripts.update(script)
        assert updated.parse_status == ScriptParseStatus.PARSING
        assert updated.last_parsed_at is not None
        assert updated.parse_location == ScriptParseLocation.AGENT


def test_case_add_many_and_list_order(client):
    container = client.app.state.container
    user_id = _seed_user_and_project(container)
    with _uow(container) as uow:
        script = uow.test_scripts.add(_make_script(user_id))
        cases = uow.script_cases.add_many(
            [
                _make_case(script.script_id, "b_case", order_index=2),
                _make_case(script.script_id, "a_case", order_index=1),
                _make_case(script.script_id, "c_case", order_index=3),
            ]
        )
        assert len(cases) == 3
        assert all(c.id is not None for c in cases)

    with _uow(container) as uow:
        listed = uow.script_cases.list_by_script(script.script_id)
        assert [c.stable_key for c in listed] == ["a_case", "b_case", "c_case"]


def test_case_duplicate_stable_key_raises(client):
    container = client.app.state.container
    user_id = _seed_user_and_project(container)
    with _uow(container) as uow:
        script = uow.test_scripts.add(_make_script(user_id))
    with pytest.raises(IntegrityError), _uow(container) as uow:
        uow.script_cases.add_many(
            [
                _make_case(script.script_id, "same"),
                _make_case(script.script_id, "same"),
            ]
        )


def test_case_update_duration_stats(client):
    """avg_duration_s / duration_samples 滚动更新（D-21 数据基础）。"""
    container = client.app.state.container
    user_id = _seed_user_and_project(container)
    with _uow(container) as uow:
        script = uow.test_scripts.add(_make_script(user_id))
        case = uow.script_cases.add(_make_case(script.script_id, "dur"))
        case.avg_duration_s = 12.5
        case.duration_samples = 4
        updated = uow.script_cases.update(case)
        assert updated.avg_duration_s == 12.5
        assert updated.duration_samples == 4


def test_case_deleted_excluded_by_default(client):
    container = client.app.state.container
    user_id = _seed_user_and_project(container)
    with _uow(container) as uow:
        script = uow.test_scripts.add(_make_script(user_id))
        uow.script_cases.add(_make_case(script.script_id, "alive"))
        dead = uow.script_cases.add(_make_case(script.script_id, "gone"))
        dead.deleted = True
        uow.script_cases.update(dead)

    with _uow(container) as uow:
        listed = uow.script_cases.list_by_script(script.script_id)
        assert [c.stable_key for c in listed] == ["alive"]
        with_deleted = uow.script_cases.list_by_script(script.script_id, include_deleted=True)
        assert {c.stable_key for c in with_deleted} == {"alive", "gone"}
