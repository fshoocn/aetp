"""台架侧脚本验证下发服务（P7.2）。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.ids import new_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import ScriptVerifyPayload, ScriptVerifyResultPayload
from aetp_protocol.topics import command_topic

from master.application.services.script_download_service import ScriptDownloadService
from master.domain.enums import OutboxStatus
from master.domain.models import DomainEvent, OutboxMessage
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow
from master.plugins.registry import PluginRegistry


@dataclass(frozen=True)
class ScriptVerificationResult:
    """Agent 验证回传的项目范围结果。"""

    verify_id: str
    project_id: str
    script_id: str
    node_id: str
    errors: list[str]
    event: DomainEvent


class ScriptVerificationService:
    """构造 script.verify 命令，并把 Agent 结果投影为领域事件。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        plugin_registry: PluginRegistry,
        script_download: ScriptDownloadService,
        *,
        master_id: str = "aetp-master",
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = plugin_registry
        self._script_download = script_download
        self._master_id = master_id

    def request(
        self,
        *,
        project_id: str,
        script_id: str,
        node_id: str,
        config: dict,
    ) -> dict:
        """向项目绑定的在线 Agent 下发验证命令。"""
        with self._uow_factory() as uow:
            script = uow.test_scripts.get_by_script_id(script_id)
            if script is None or script.project_id != project_id:
                raise ValueError("脚本不存在或不属于当前项目")
            binding = uow.bindings.get(project_id, node_id)
            if binding is None or not binding.enabled:
                raise ValueError("节点未绑定到项目或绑定已禁用")
            node = uow.nodes.get_by_id(node_id)
            if node is None or not node.online or not node.enabled:
                raise ValueError("节点当前不在线或已禁用")
            package = self._registry.require(script.task_type)
            agent = package.agent
            if getattr(agent, "verify_location", "master") != "agent":
                raise ValueError("该插件未声明 Agent 台架验证能力")

            verify_id = new_id()
            payload = ScriptVerifyPayload(
                verify_id=verify_id,
                script_id=script.script_id,
                project_id=project_id,
                version=script.version,
                task_type=script.task_type,
                plugin_version=script.plugin_version,
                script_ref={
                    "script_id": script.script_id,
                    "version": script.version,
                    "sha256": script.sha256,
                    "download_url": self._script_download.build_download_url(script.script_id),
                },
                config=dict(config or script.config or {}),
            )
            envelope = Envelope(
                message_id=new_id(),
                message_type=MessageType.SCRIPT_VERIFY.value,
                sent_at=utcnow(),
                sender=Sender(
                    kind=SenderKind.MASTER,
                    id=self._master_id,
                    session_id=self._master_id,
                ),
                trace_id=verify_id,
                payload=payload.model_dump(mode="json"),
            )
            uow.outbox_messages.enqueue(
                OutboxMessage(
                    outbox_id=f"script-verify:{verify_id}",
                    aggregate_type="script_verification",
                    aggregate_id=verify_id,
                    topic=command_topic(node_id, "verify"),
                    payload=envelope.model_dump(mode="json"),
                    qos=1,
                    status=OutboxStatus.PENDING,
                    next_attempt_at=None,
                )
            )
            uow.domain_events.add(
                DomainEvent(
                    event_id=new_id(),
                    project_id=project_id,
                    event_type="script.verify_requested",
                    aggregate_id=verify_id,
                    payload={
                        "verify_id": verify_id,
                        "script_id": script_id,
                        "node_id": node_id,
                        "task_type": script.task_type,
                    },
                )
            )
        return {
            "verify_id": verify_id,
            "project_id": project_id,
            "script_id": script_id,
            "node_id": node_id,
            "status": "dispatched",
        }

    def handle_result(self, node_id: str, payload: ScriptVerifyResultPayload) -> ScriptVerificationResult | None:
        """持久化 Agent 验证结果，供 SSE/插件 UI 查询。"""
        with self._uow_factory() as uow:
            request_event = next(
                (
                    event
                    for event in reversed(uow.domain_events.list(limit=10000))
                    if event.event_type == "script.verify_requested" and event.aggregate_id == payload.verify_id
                ),
                None,
            )
            if request_event is None:
                raise ValueError(f"未知 script.verify 请求: {payload.verify_id}")
            request = request_event.payload
            project_id = request_event.project_id or ""
            expected_script_id = str(request.get("script_id", ""))
            expected_node_id = str(request.get("node_id", ""))
            if payload.script_id != expected_script_id:
                raise ValueError("script.verify-result 与原请求脚本不匹配")
            if node_id != expected_node_id:
                raise ValueError("script.verify-result 来源节点与原请求不匹配")
            if payload.project_id != project_id:
                raise ValueError("script.verify-result 项目与原请求不匹配")
            duplicate = next(
                (
                    event
                    for event in reversed(uow.domain_events.list(project_id=project_id, limit=10000))
                    if event.event_type == "script.verify_result" and event.aggregate_id == payload.verify_id
                ),
                None,
            )
            if duplicate is not None:
                return None
            result_event = uow.domain_events.add(
                DomainEvent(
                    event_id=new_id(),
                    project_id=project_id,
                    event_type="script.verify_result",
                    aggregate_id=payload.verify_id,
                    payload={
                        "verify_id": payload.verify_id,
                        "script_id": payload.script_id,
                        "node_id": node_id,
                        "errors": list(payload.errors),
                    },
                )
            )

        result = ScriptVerificationResult(
            verify_id=payload.verify_id,
            project_id=project_id,
            script_id=payload.script_id,
            node_id=node_id,
            errors=list(payload.errors),
            event=result_event,
        )
        return result

    def get_result(self, project_id: str, verify_id: str) -> dict | None:
        """查询指定项目的最近验证结果。"""
        with self._uow_factory() as uow:
            events = uow.domain_events.list(project_id=project_id, limit=1000)
        for event in reversed(events):
            if event.event_type == "script.verify_result" and event.aggregate_id == verify_id:
                return dict(event.payload)
        return None
