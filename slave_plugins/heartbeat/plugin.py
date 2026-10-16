"""心跳插件入口：向主模块周期性上报本节点存活状态。

职责：

1. 从 ``config.ini`` 的 ``[heartbeat]`` 节读主模块地址与心跳参数
   （见 :mod:`slave_plugins.heartbeat.config`）；
2. 启动心跳循环（:class:`~slave_plugins.heartbeat.monitor.HeartbeatMonitor`），
   覆盖主模块不在线 / 掉线 / 上线三种场景，失败自动退避重试；
3. 在本节点 ``ctx.webapi`` 注册 ``GET /api/heartbeat/status`` 自省接口
   （返回值带 ``extensions`` 展示扩展槽与 ``capabilities`` 能力信封列表，
   其他插件可经 :data:`STATUS_EVENT` / ``heartbeat/collect-capabilities``
   事件插入自家数据）；
4. 卸载时取消心跳循环，不留后台任务。

配置（``slave/config.ini``）::

    [heartbeat]
    master_url = http://127.0.0.1:8080
    interval = 5.0
    timeout = 3.0
    max_backoff = 60.0

其中 ``interval`` / ``timeout`` / ``max_backoff`` 是**启动兜底值** ——
真正生效的运行参数由主模块在心跳响应的 ``config`` 字段里下发，主模块
改配置后最迟一个心跳周期自动同步到本模块（见
:meth:`~slave_plugins.heartbeat.config.HeartbeatConfig.apply_server`）。

状态机与退避策略见 :mod:`slave_plugins.heartbeat.monitor`；传输层
（标准库 urllib、异常归一化）见 :mod:`slave_plugins.heartbeat.sender`。
"""

from __future__ import annotations

import asyncio
import time
from typing import ClassVar

from cordis_port import Context, EventDispatchError
from starlette.requests import Request

from .capabilities import CapProblemAlerter, capabilities_digest, collect_capabilities
from .config import HeartbeatConfig
from .monitor import HeartbeatMonitor, MasterState
from .sender import HeartbeatSender, HeartbeatSendError

#: 状态接口的扩展收集事件：``GET /api/heartbeat/status`` 构造好响应后派发它，
#: 并把**响应 dict** 传给监听器；其他插件把自家数据写进
#: ``response["extensions"]["<插件名>"]`` 即会出现在返回值里。
STATUS_EVENT = "heartbeat/collect-status"

#: 心跳序号上限：取 JSON / JS 消费端的安全整数上限（``2**53 - 1``)——
#: Python 的 int 虽无界，但主模块侧（JS / 浏览器）超过此值会丢精度。
#: 到顶后**封顶**不再递增：不回绕到 1，否则会被主模块误判为「从模块重启」。
SEQ_MAX: int = 2**53 - 1


def next_seq(current: int) -> int:
    """返回下一个心跳序号：小于 :data:`SEQ_MAX` 时加一，否则停在上限。"""
    return current + 1 if current < SEQ_MAX else SEQ_MAX


class HeartbeatPlugin:
    """心跳上报插件。

    ``inject`` 注入的是**服务**（不是插件名，别和 ``plugin.json`` 的
    ``requires`` 混淆）：

    * ``appconfig`` —— 应用配置服务，由 ``AppConfigPlugin`` 提供；
    * ``webapi`` —— Web API 服务，由 ``WebApiPlugin`` 提供；
    * ``node_kind`` —— 节点类型值服务，由 ``common/node_runtime.py`` 装配时
      ``ctx.provide("node_kind", kind)`` 注册，**没有对应插件**；
    * ``node_name`` —— 节点名值服务，同样由装配阶段注册（``[node] name``
      缺省取本机设备名），心跳上报的 ``node_id`` 即取自此服务。
    """

    name: ClassVar[str] = "heartbeat"
    inject: ClassVar[list[str]] = ["appconfig", "webapi", "node_kind", "node_name"]
    #: 配置模型（标准 Schema）：驱动校验、``[heartbeat]`` 默认值注入与写回
    Config: ClassVar[type[HeartbeatConfig]] = HeartbeatConfig

    def __init__(self, ctx: Context, config: HeartbeatConfig | None = None) -> None:
        self.ctx: Context = ctx
        self.config: HeartbeatConfig = (
            config if isinstance(config, HeartbeatConfig) else HeartbeatConfig()
        )
        self._node_id: str = str(ctx.node_name)
        self._seq: int = 0
        self._task: asyncio.Task[None] | None = None
        self._skip_warned: dict[str, str] = {}
        #: 能力问题告警去重（「消失即复位」语义，见 :class:`CapProblemAlerter`）
        self._cap_alerts: CapProblemAlerter = CapProblemAlerter(self._log_cap_problem)
        #: 已确认送达主模块的能力摘要；``None`` 表示主模块缺当前能力集
        self._caps_sent_digest: str | None = None
        self._sender: HeartbeatSender = HeartbeatSender(self.config)
        self.monitor: HeartbeatMonitor = HeartbeatMonitor(
            self.config,
            self._send,
            self._build_payload,
            on_transition=self._log_transition,
            on_config=self._log_config,
        )

    # -- 心跳载荷 -----------------------------------------------------------
    async def _build_payload(self) -> dict[str, object]:
        """构造一次心跳上报的 JSON 载荷（含能力信封）。

        ``seq`` 每次递增供主模块检测重启 / 丢包；到达 :data:`SEQ_MAX` 后
        封顶（不回绕）。

        能力同步用「摘要 + 按需全量」：平时只带 ``caps_digest``，摘要变了
        或主模块回 ``need_caps`` 时才带全量 ``capabilities``（能力信息变化
        频率低，不必每拍重发）。信封契约见
        :mod:`slave_plugins.heartbeat.capabilities`。
        """
        self._seq = next_seq(self._seq)
        entries, problems = await collect_capabilities(self.ctx)
        self._cap_alerts.report(problems)
        digest = capabilities_digest(entries)
        payload: dict[str, object] = {
            "node_id": self._node_id,
            "node_kind": str(self.ctx.node_kind),
            "address": self.ctx.webapi.address() or "",
            "seq": self._seq,
            "sent_at": time.time(),
            "status": "alive",
            "caps_digest": digest,
        }
        if digest != self._caps_sent_digest:
            payload["capabilities"] = entries
        return payload

    async def _send(self, payload: dict[str, object]) -> dict[str, object]:
        """发送心跳并维护能力同步状态（摘要已送达 / 主模块要全量）。"""
        try:
            response = await self._sender.send(payload)
        except HeartbeatSendError:
            # 送达不确定：下一拍重发全量（宁多勿缺）
            self._caps_sent_digest = None
            raise
        if bool(response.get("need_caps")):
            self._caps_sent_digest = None
        elif "capabilities" in payload:
            digest = payload.get("caps_digest")
            if isinstance(digest, str):
                self._caps_sent_digest = digest
        return response

    def _log_cap_problem(self, problem: str) -> None:
        self.ctx.logger.warning("能力收集：%s", problem)

    # -- 生命周期 -----------------------------------------------------------
    async def init(self) -> object:
        """注册自省接口并启动心跳循环；返回值即卸载清理函数。"""
        self.ctx.webapi.get(
            "/api/heartbeat/status",
            self._status,
            kind="api",
            name="heartbeat.status",
        )
        self._task = asyncio.create_task(self.monitor.run())
        self._task.add_done_callback(self._log_task_exit)
        self.ctx.logger.info(
            "心跳插件已启动：主模块 %s，间隔 %s 秒",
            self.config.endpoint(),
            self.config.interval,
        )
        return self._stop

    def _log_task_exit(self, task: asyncio.Task[None]) -> None:
        """心跳任务退出的观察哨：意外退出记 error 日志，不再无声消失。

        ``task.exception()`` 在已取消的任务上会抛 ``CancelledError``，
        所以先判 :meth:`Task.cancelled`；正常返回 / 被取消都不算异常。
        """
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("心跳任务意外退出：%s", error)

    async def _stop(self) -> None:
        """卸载清理：取消心跳循环并等待其退出。

        用 ``gather(return_exceptions=True)``：心跳任务自身的 CancelledError
        被吸收，而清理链若被外部取消仍会正常向上抛出。
        """
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # -- 自省接口 -----------------------------------------------------------
    async def _status(self, _request: Request) -> dict[str, object]:
        """``GET /api/heartbeat/status``：状态快照 + 其他插件的扩展数据。

        扩展契约：派发 :data:`STATUS_EVENT` 并把响应 dict 传给监听器，
        其他插件把自家数据写进 ``response["extensions"]["<插件名>"]``
        （核心字段只读，避免撞名）；监听器同步 / 异步均可，**并发**派发，
        某个贡献者出错不影响本接口 —— 错误进 ``response["extension_errors"]``
        （恒在，无错时为空列表；非法能力条目的问题也汇入这里），其余贡献
        照常返回。能力信封列表见 ``response["capabilities"]`` / ``caps_digest``。
        """
        response = self.monitor.snapshot().to_dict()
        extensions: dict[str, object] = {}
        errors: list[str] = []
        response["extensions"] = extensions
        response["extension_errors"] = errors
        entries, problems = await collect_capabilities(self.ctx)
        response["capabilities"] = entries
        response["caps_digest"] = capabilities_digest(entries)
        errors.extend(problems)
        try:
            await self.ctx.parallel(STATUS_EVENT, response)
        except EventDispatchError as exc:
            errors.extend(f"{type(error).__name__}: {error}" for error in exc.errors)
        return response

    # -- 状态转换日志 -------------------------------------------------------
    def _log_transition(
        self, previous: MasterState, current: MasterState, error: str | None
    ) -> None:
        """只在状态转换时打日志，失败重试本身不刷屏。"""
        if current is MasterState.OFFLINE:
            if previous is MasterState.ONLINE:
                self.ctx.logger.warning(
                    "主模块连接已断开：%s；进入退避重试（最长 %s 秒一次）",
                    error or "未知错误",
                    self.config.max_backoff,
                )
            else:
                self.ctx.logger.warning(
                    "主模块不在线：%s；进入退避重试（最长 %s 秒一次）",
                    error or "未知错误",
                    self.config.max_backoff,
                )
        elif current is MasterState.ONLINE:
            if previous is MasterState.OFFLINE:
                self.ctx.logger.info(
                    "主模块连接已恢复：%s", self.config.endpoint()
                )
            else:
                self.ctx.logger.info("主模块连接已建立：%s", self.config.endpoint())

    # -- 配置同步日志 -------------------------------------------------------
    def _log_config(
        self,
        applied: dict[str, tuple[float, float]],
        skipped: dict[str, str],
    ) -> None:
        """主模块下发配置变更时记录一条日志（不逐值刷屏）。

        被跳过项按 ``(键, 原因)`` 去重告警：主模块持续下发同一个坏值时
        不会每个心跳周期刷一条 warning。
        """
        for name, (old, new) in applied.items():
            self.ctx.logger.info(
                "主模块下发配置：%s %s → %s", name, old, new
            )
        for name, reason in skipped.items():
            if self._skip_warned.get(name) == reason:
                continue  # 同键同因已告警过，不重复刷屏
            self._skip_warned[name] = reason
            self.ctx.logger.warning(
                "主模块下发配置被跳过：%s（%s）", name, reason
            )
