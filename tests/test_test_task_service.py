"""P4.5 延伸：创建任务定义时的节点筛选测试（D-23 两层绑定软校验）。

验收（§18.5 保存定义时校验）：node_ids ⊆ 项目绑定（越界 → PROJECT_ACCESS_DENIED）；
从引用脚本读硬件需求对选中节点做能力匹配；无满足节点仅告警（软校验，不阻止保存）。
"""

from __future__ import annotations

import pytest
from aetp_protocol.capabilities import (
    BusRequirement,
    HardwareChannel,
    HardwareRequirements,
    NodeCapabilities,
    VehicleBus,
    VehicleCapability,
    VehicleRequirement,
    VehicleVendor,
)

from master.application.errors import ProjectAccessDeniedError, ScriptNotFoundError
from master.domain.enums import (
    AccountStatus,
    NodeStatus,
    PlatformRole,
    ProjectStatus,
    ScriptParseLocation,
    ScriptParseStatus,
)
from master.domain.models import (
    Node,
    Project,
    ProjectNodeBinding,
    TestScript,
    User,
)
from master.domain.time import utcnow


def _uow(container):
    return container.uow_factory()()


def _seed(container, *, capabilities=None, tags=None) -> None:
    """建用户 + 项目 + 节点（bench-001） + 绑定 + 脚本（含硬件需求）。"""
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
        uow.nodes.save(
            Node(
                id=None,
                node_id="bench-001",
                name="CAN 台架",
                hostname="h",
                status=NodeStatus.ONLINE,
                online=True,
                enabled=True,
                capabilities=capabilities
                if capabilities is not None
                else NodeCapabilities(
                    vehicle=VehicleCapability(
                        vendors=(
                            VehicleVendor(
                                name="vector",
                                buses=(
                                    VehicleBus(
                                        bus_type="can",
                                        channels=(
                                            HardwareChannel(name="can0"),
                                            HardwareChannel(name="can1"),
                                            HardwareChannel(name="can2"),
                                            HardwareChannel(name="can3"),
                                        ),
                                    ),
                                ),
                            ),
                        )
                    )
                ),
                tags=tags if tags is not None else ["can"],
            )
        )
        uow.bindings.add(
            ProjectNodeBinding(
                id=None,
                project_id="p1",
                node_id="bench-001",
                enabled=True,
                assigned_by=user.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        uow.test_scripts.add(
            TestScript(
                project_id="p1",
                script_id="S-1",
                task_type="canoe",
                name="reg",
                version=1,
                file_ref="data/scripts/S-1/1/",
                size=1024,
                sha256="a" * 64,
                config={},
                hardware_requirements=HardwareRequirements(
                    vehicle=VehicleRequirement(all_of=(BusRequirement(bus_type="can", minimum_channels=2),)),
                    required_tags=("can",),
                ),
                parse_status=ScriptParseStatus.PARSED,
                parse_location=ScriptParseLocation.MASTER,
                result_parse_location=ScriptParseLocation.MASTER,
                plugin_version="1.0.0",
                created_by=user.id,
            )
        )


def _service(container):
    return container.test_task_service()


def test_validate_selection_all_match(client):
    """全部选中节点满足硬件需求 → matched，无告警。"""
    container = client.app.state.container
    _seed(container)
    svc = _service(container)

    result = svc.validate_node_selection("p1", ["bench-001"], "S-1")
    assert result.matched_count == 1
    assert result.matches[0].matched is True
    assert result.warning is None


def test_validate_selection_partial_match_with_failures(client):
    """部分满足：明细含不满足节点的失败原因。"""
    container = client.app.state.container
    _seed(container)
    # 再建一个能力不足的节点并绑定
    with _uow(container) as uow:
        uow.nodes.save(
            Node(
                id=None,
                node_id="bench-002",
                name="弱台架",
                hostname="h2",
                status=NodeStatus.ONLINE,
                online=True,
                enabled=True,
                capabilities=NodeCapabilities(
                    vehicle=VehicleCapability(
                        vendors=(
                            VehicleVendor(
                                name="vector",
                                buses=(
                                    VehicleBus(
                                        bus_type="can",
                                        channels=(HardwareChannel(name="can0"),),
                                    ),
                                ),
                            ),
                        )
                    )
                ),
                tags=["can"],
            )
        )
        uow.bindings.add(
            ProjectNodeBinding(
                id=None,
                project_id="p1",
                node_id="bench-002",
                enabled=True,
                assigned_by=1,  # _seed 创建的 uploader（首个用户 id=1）
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
    svc = _service(container)

    result = svc.validate_node_selection("p1", ["bench-001", "bench-002"], "S-1")
    assert result.matched_count == 1
    by_node = {m.node_id: m for m in result.matches}
    assert by_node["bench-001"].matched is True
    assert by_node["bench-002"].matched is False
    assert by_node["bench-002"].failures  # 含"期望 ≥ 2，实际 1"等
    assert result.warning is None  # 仍有节点满足 → 不告警


def test_validate_selection_no_match_warns_but_does_not_raise(client):
    """无满足节点：软校验仅告警，不阻止保存（触发时才硬校验）。"""
    container = client.app.state.container
    _seed(
        container,
        capabilities=NodeCapabilities(
            vehicle=VehicleCapability(
                vendors=(
                    VehicleVendor(
                        name="vector",
                        buses=(
                            VehicleBus(
                                bus_type="can",
                                channels=(HardwareChannel(name="can0"),),
                            ),
                        ),
                    ),
                )
            )
        ),
        tags=["other"],
    )

    result = _service(container).validate_node_selection("p1", ["bench-001"], "S-1")
    assert result.matched_count == 0
    assert result.warning is not None
    assert "NODE_CAPABILITY_MISMATCH" in result.warning


def test_validate_selection_rejects_node_outside_binding(client):
    """验收：node_ids 越界（非项目绑定节点）→ PROJECT_ACCESS_DENIED（D-23 第一层）。"""
    container = client.app.state.container
    _seed(container)
    svc = _service(container)

    with pytest.raises(ProjectAccessDeniedError) as exc_info:
        svc.validate_node_selection("p1", ["bench-001", "bench-999"], "S-1")
    assert exc_info.value.code == "PROJECT_ACCESS_DENIED"


def test_validate_selection_rejects_script_from_other_project(client):
    """脚本不存在或不属于当前项目 → ScriptNotFoundError（IDOR 防护）。"""
    container = client.app.state.container
    _seed(container)
    svc = _service(container)

    with pytest.raises(ScriptNotFoundError):
        svc.validate_node_selection("p1", ["bench-001"], "S-other")
