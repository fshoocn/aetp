"""通知 immediate/run_summary/digest 策略测试。"""

from __future__ import annotations

from datetime import timedelta

from master.domain.time import utcnow
from tests.test_notifications import _create_admin, _create_project


def _create_subscription(client, project_id: str, headers: dict[str, str], policy: dict, event_types: list[str]) -> str:
    endpoint = client.post(
        f"/api/v1/projects/{project_id}/notification-endpoints",
        headers=headers,
        json={"channel_type": "console_test", "name": f"endpoint-{policy.get('mode', 'immediate')}"},
    )
    assert endpoint.status_code == 201, endpoint.text
    response = client.post(
        f"/api/v1/projects/{project_id}/event-subscriptions",
        headers=headers,
        json={
            "endpoint_id": endpoint.json()["endpoint_id"],
            "event_types": event_types,
            "throttle_policy": policy,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["subscription_id"]


def test_run_summary_persists_progress_until_terminal_event(client) -> None:
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "NOTIF_SUMMARY")
    subscription_id = _create_subscription(
        client,
        project_id,
        headers,
        {"mode": "run_summary", "max_items": 2},
        ["run.progress", "run.result"],
    )
    publisher = client.app.state.container.event_publisher()

    import asyncio

    asyncio.run(
        publisher.publish(
            "run.progress",
            {"run_id": "RUN-SUMMARY", "task_id": "TASK-1", "percent": 40},
            project_id=project_id,
            aggregate_id="RUN-SUMMARY",
        )
    )
    with client.app.state.container.uow_factory()() as uow:
        pending = uow.event_deliveries.list_by_subscription(
            project_id,
            subscription_id,
            status="aggregated",
            aggregation_key="run:RUN-SUMMARY",
        )
    assert len(pending) == 1

    asyncio.run(
        publisher.publish(
            "run.result",
            {"run_id": "RUN-SUMMARY", "task_id": "TASK-1", "passed": True},
            project_id=project_id,
            aggregate_id="RUN-SUMMARY",
        )
    )
    with client.app.state.container.uow_factory()() as uow:
        records = uow.event_deliveries.list_by_subscription(project_id, subscription_id)
    assert sorted(record.status for record in records) == ["coalesced", "succeeded"]
    sent = next(record for record in records if record.status == "succeeded")
    assert sent.item_count == 2
    assert len(sent.content["events"]) == 2


def test_digest_flushes_persisted_window(client) -> None:
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "NOTIF_DIGEST")
    subscription_id = _create_subscription(
        client,
        project_id,
        headers,
        {"mode": "digest", "window_s": 60, "max_items": 2},
        ["run.progress"],
    )
    publisher = client.app.state.container.event_publisher()

    import asyncio

    for percent in (10, 20):
        asyncio.run(
            publisher.publish(
                "run.progress",
                {"run_id": "RUN-DIGEST", "task_id": "TASK-2", "percent": percent},
                project_id=project_id,
                aggregate_id="RUN-DIGEST",
            )
        )

    dispatcher = client.app.state.container.notification_dispatcher()
    assert asyncio.run(dispatcher.flush_due(now=utcnow() + timedelta(minutes=2))) == 1
    with client.app.state.container.uow_factory()() as uow:
        records = uow.event_deliveries.list_by_subscription(project_id, subscription_id)
    assert sorted(record.status for record in records) == ["coalesced", "succeeded"]
    sent = next(record for record in records if record.status == "succeeded")
    assert sent.item_count == 2
    assert sent.content["event_type"] == "notification.digest"
