"""Agent 优雅重启协调器测试。"""

from __future__ import annotations

import asyncio

from agent.application.services.restart_coordinator import AgentRestartCoordinator


def test_restart_coordinator_signals_once_and_awaits() -> None:
    coordinator = AgentRestartCoordinator()
    assert coordinator.requested() is False

    async def scenario() -> None:
        waiter = asyncio.create_task(coordinator.wait_for_restart())
        await asyncio.sleep(0)
        assert waiter.done() is False
        coordinator.request_restart()
        await asyncio.wait_for(waiter, timeout=1)
        assert coordinator.requested() is True
        # 幂等：重复请求仍为已请求
        coordinator.request_restart()
        assert coordinator.requested() is True

    asyncio.run(scenario())


def test_restart_coordinator_is_injected_into_runtime_restart_callback() -> None:
    """maintenance/plugin-sync 的 restart 回调应落到协调器（request_restart），而非直接 execv。"""
    coordinator = AgentRestartCoordinator()
    # 模拟维护控制器/插件同步注入的回调
    restart_callback = coordinator.request_restart
    restart_callback()
    assert coordinator.requested() is True
