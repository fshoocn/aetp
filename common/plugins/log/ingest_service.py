"""log 的远端入库服务（``ctx.log_ingest``）：``log.batch`` 消息落本地查询库。

comm 是晚到服务（预装阶段才装），远端入库桥接独立成本服务、
``inject = ["log", "comm"]`` **注入式挂载**——两者就位 cordis 才注册
``log.batch`` 处理器（依赖缺席安静 PENDING；comm 换装 / 重载随依赖纪元
自动重挂），不把早装的日志核心绑在 comm 的装载时序上。处理器注册绑定
调用方 fiber，``comm.on`` 自带随 fiber 注销，无需手动清理。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from cordis_port import Context, Service

from common.contracts import CommMeta

if TYPE_CHECKING:  # 运行期无环：plugin 导入本模块装载子服务
    from .plugin import LogPlugin


class LogIngestService(Service[None]):
    """log 远端入库服务：把 ``log.batch`` 消息写入本地查询库。"""

    #: 附属服务标记：本类是 ``log`` 插件的界面层载体之一，不是独立插件——
    #: 插件清单（builtin_specs）据此不单列，随宿主插件 log 一起呈现
    service_of: ClassVar[str] = "log"
    #: 服务名（即 ``ctx.log_ingest``）
    provide: str | None = "log_ingest"
    #: fiber 名（诊断用）
    name: str = "log_ingest"
    #: 远端入库的依赖集 = 自家核心 ``log`` + 通信 ``comm``：两者就位才装载
    inject: ClassVar[list[str]] = ["log", "comm"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.log_ingest
        self.ctx: Context = ctx

    def init(self) -> None:
        """注册 ``log.batch`` 处理器（依赖集由 fiber 保证就绪，无需轮询）。

        ``comm.on`` 把注销挂在调用方 fiber 上：本服务卸载 / 重载自动摘除。
        """
        self.ctx.comm.on("log.batch", self._on_batch)

    def _on_batch(self, payload: Any, meta: CommMeta) -> None:
        cast("LogPlugin", self.ctx.get("log")).ingest_remote(payload, meta)


__all__: list[str] = ["LogIngestService"]
