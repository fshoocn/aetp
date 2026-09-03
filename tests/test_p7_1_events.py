from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import cast

from starlette.requests import Request

from master.api.permissions import ProjectAccess
from master.domain.enums import ProjectRole, ProjectStatus
from master.domain.models import DomainEvent, Project, ProjectMember
from master.domain.time import utcnow


def _uow(container):
    return container.uow_factory()()


def _seed_project_member(container, project_id: str = "p-events") -> None:
    with _uow(container) as uow:
        user = uow.users.get_by_username("tester")
        assert user is not None
        uow.projects.add(
            Project(
                id=None,
                project_id=project_id,
                project_key=project_id.upper(),
                name="Events",
                description="",
                status=ProjectStatus.ACTIVE,
                created_by=user.persisted_id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        uow.members.add(
            ProjectMember(
                id=None,
                project_id=project_id,
                user_id=user.persisted_id,
                project_role=ProjectRole.VIEWER,
                assigned_by=user.persisted_id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )


def test_event_publisher_persists_and_filters_project_subscribers(client) -> None:
    container = client.app.state.container
    publisher = container.event_publisher()
    bus = container.event_bus()

    async def scenario():
        p1 = await bus.subscribe("p1")
        p2 = await bus.subscribe("p2")
        try:
            event = await publisher.publish(
                "run.created",
                {"project_id": "p1", "run_id": "R-1"},
            )
            received = await asyncio.wait_for(p1.get(), timeout=1)
            assert received.sequence == event.sequence
            assert received.project_id == "p1"
            try:
                await asyncio.wait_for(p2.get(), timeout=0.05)
            except TimeoutError:
                pass
            else:
                raise AssertionError("p2 不应收到 p1 事件")
        finally:
            await bus.unsubscribe(p1)
            await bus.unsubscribe(p2)

    asyncio.run(scenario())

    with _uow(container) as uow:
        events = uow.domain_events.list(project_id="p1")
        assert len(events) == 1
        assert events[0].event_type == "run.created"
        assert events[0].payload["run_id"] == "R-1"
        assert events[0].sequence is not None


def test_sse_replays_events_after_last_event_id(client, auth_header) -> None:
    _seed_project_member(client.app.state.container)
    publisher = client.app.state.container.event_publisher()
    asyncio.run(publisher.publish("run.created", {"project_id": "p-events", "run_id": "R-1"}))
    second = asyncio.run(publisher.publish("run.updated", {"project_id": "p-events", "run_id": "R-1"}))
    assert second.sequence is not None

    from master.api.events import stream_events

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v2/events",
        "headers": [(b"last-event-id", str(second.sequence - 1).encode())],
        "query_string": b"project_id=p-events",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "root_path": "",
        "http_version": "1.1",
    }

    async def scenario():
        request = Request(scope)
        with _uow(client.app.state.container) as uow:
            user = uow.users.get_by_username("tester")
        assert user is not None
        response = await stream_events(
            request,
            project_id="p-events",
            _access=ProjectAccess(user, ProjectRole.VIEWER, False),
            uow_factory=client.app.state.container.uow_factory(),
            event_bus=client.app.state.container.event_bus(),
        )
        iterator = cast(AsyncGenerator[str, None], response.body_iterator)
        try:
            assert (await anext(iterator)).startswith(": connected")
            event_chunk = await anext(iterator)
            return event_chunk
        finally:
            await iterator.aclose()

    chunk = asyncio.run(scenario())
    lines = chunk.splitlines()
    assert f"id: {second.sequence}" in lines
    payload = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
    assert payload["sequence"] == second.sequence
    assert payload["project_id"] == "p-events"
    assert payload["type"] == "run.updated"


def test_sse_requires_project_membership(client, auth_header) -> None:
    response = client.get(
        "/api/v2/events?project_id=not-a-member",
        headers=auth_header,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


def test_event_publisher_enqueues_event_hook_after_persisting(client) -> None:
    """EventPublisher 提交事件后把事件入队到 EventHookWorker，异步执行并审计。"""
    from master.application.services.event_publisher import EventPublisher
    from master.application.services.hook_runner import HookRegistry, HookRunner
    from master.workers.event_hook_worker import EventHookWorker

    class RecordingHook:
        name = "recording-event-hook"
        event_types = frozenset({"run.created"})

        def __init__(self) -> None:
            self.event_ids: list[str] = []

        async def handle(self, event: DomainEvent) -> None:
            self.event_ids.append(event.event_id)

    hook = RecordingHook()
    container = client.app.state.container
    runner = HookRunner(
        lambda: container.uow_factory()(),
        registry=HookRegistry(event_hooks=[hook]),
    )
    worker = EventHookWorker(runner)
    publisher = EventPublisher(
        container.uow_factory(),
        container.event_bus(),
        event_hook_worker=worker,
    )

    event = asyncio.run(publisher.publish("run.created", {"project_id": "p-hook", "run_id": "R-hook"}))

    # 事件已非阻塞入队；驱动一次消费后 hook 被执行并写审计
    asyncio.run(worker.drain_once())

    assert hook.event_ids == [event.event_id]
    with _uow(container) as uow:
        executions = uow.hook_executions.list_by_project("p-hook")
    assert len(executions) == 1
    assert executions[0].event_id == event.event_id
    assert executions[0].status == "succeeded"
