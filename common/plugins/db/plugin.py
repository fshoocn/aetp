"""数据库插件入口：装配引擎、建库内元表并挂数据自检接口。

加载后其他插件声明 ``inject = ["db"]`` 即可建表与增删改查::

    class MyPlugin:
        inject = ["db"]

        async def init(self):
            runs = ctx.db.repo(TestRun)
            await ctx.db.sync_schema()

生命周期语义（DESIGN §4.3，关键）：

* **清理绝不挂 fiber dispose** —— cordis 的 ``fiber.dispose()`` 不区分
  热更新与停机，挂在上面会「改一次配置、丢一次数据」；卸载清理只走
  插件管理器的显式链路（``ctx.plugins.uninstall`` → ``ctx.db.drop_owner``）；
* fiber 卸载的 effect 只负责**排空连接池**（热更新 / 停机都安全：不动表）；
* **孤儿数据不随启动回收**（启动快照会受装载顺序影响、误判内置插件）——
  改由自检接口手动触发：``ctx.db.self_check()`` / ``POST /api/db/self-check``
  （报告孤儿数据，``policy=drop`` 才清理）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar, cast

from cordis_port import Context
from starlette.requests import Request

from .config import DbConfig
from .service import DbService


class DbPlugin:
    """db 插件：依赖 ``ctx.appconfig`` 读 ``[db]`` 节，注册 ``ctx.db`` 服务。"""

    #: 插件名（即插件 id）
    name: ClassVar[str] = "db"
    #: 展示名与版本：内置插件没有 plugin.json，清单元数据由插件类属性提供
    display_name: ClassVar[str] = "数据库服务"
    version: ClassVar[str] = "1.0.0"
    #: 依赖应用配置（读 ``[db]`` 节与 ``[node] install_root``）
    inject: ClassVar[list[str]] = ["appconfig"]
    #: 配置模型（标准 Schema）：驱动校验、默认值注入与写回
    Config: ClassVar[type[DbConfig]] = DbConfig

    def __init__(self, ctx: Context, config: DbConfig | None = None) -> None:
        self.ctx: Context = ctx
        self.config: DbConfig = config if isinstance(config, DbConfig) else DbConfig()
        self.service: DbService = DbService(
            ctx,
            self.config,
            install_root=_install_root(ctx),
            base_dir=_base_dir(ctx),
        )
        self._route_task: asyncio.Task[None] | None = None

    async def init(self) -> None:
        """验证连接、建库内元表并挂自检接口（失败即启动失败，快速暴露）。"""
        await self.service.start()
        self._route_task = asyncio.create_task(self._mount_self_check_route())
        # 「排空连接池」的清理函数：effect 的**返回值**在 fiber 卸载时被调用
        # （注意不是调用）；只撤路由任务、动连接池，不动表。
        self.ctx.fiber.effect(self._make_cleanup, "plugins.db.shutdown")

    def _make_cleanup(self) -> Callable[[], Awaitable[None]]:
        async def cleanup() -> None:
            task, self._route_task = self._route_task, None
            if task is not None:
                task.cancel()
            await self.service.shutdown()

        return cleanup

    async def _mount_self_check_route(self) -> None:
        """延迟挂载 ``POST /api/db/self-check``（db 装载早于 webapi）。

        与 log 插件同款：轮询等 webapi 就绪即挂；无头部署 30 秒后放弃。
        """
        for _attempt in range(60):
            api = self.ctx.get("webapi")
            if api is not None:
                api.post(
                    "/api/db/self-check",
                    self._self_check,
                    kind="api",
                    name="db.self-check",
                )
                return
            await asyncio.sleep(0.5)

    async def _self_check(self, request: Request) -> dict[str, object]:
        """``POST /api/db/self-check``：手动触发数据自检。

        请求体可选 ``{"policy": "warn" | "drop"}``（缺省取 ``[db]
        orphan_policy``）：``warn`` 只报告，``drop`` 清理孤儿。
        """
        policy = str(self.config.orphan_policy)
        raw = await request.body()
        if raw:
            try:
                parsed: object = json.loads(raw)
            except ValueError:
                return self._error("请求体不是合法 JSON", 400)
            if isinstance(parsed, dict):
                body = cast(dict[str, object], parsed)
                if "policy" in body:
                    policy = str(body.get("policy") or "")
        try:
            return await self.service.self_check(policy=policy)
        except ValueError as exc:
            return self._error(str(exc), 400)

    def _error(self, message: str, status_code: int) -> dict[str, object]:
        # ctx.get 免注入通道（db 不能 inject webapi：装载顺序在 webapi 之前；
        # 也严禁 import webapi 插件包——插件间只经 cordis 服务往来）
        api = self.ctx.get("webapi")
        return api.json({"error": message}, status_code=status_code)


def _install_root(ctx: Context) -> Path | None:
    """从应用配置读插件安装目录（``[node] install_root``）；缺失返回 ``None``。

    相对路径按节点目录（config.ini 所在目录）解析——与 ``plugins`` 及 webapi
    的约定一致，不随启动时的工作目录漂移。
    """
    raw = ctx.appconfig.get("node", "install_root")
    if not raw or not str(raw).strip():
        return None
    path = Path(str(raw).strip()).expanduser()
    if not path.is_absolute():
        path = _base_dir(ctx) / path
    return path.resolve()


def _base_dir(ctx: Context) -> Path:
    """节点目录（config.ini 所在目录）：db 相对库路径 / data_dir 的解析基准。

    与 ``install_root`` / ``plugins`` 的相对路径约定一致（按配置文件所在
    目录解析），默认库文件因此落节点目录下的 ``data/``（如 ``master/data/``）。
    """
    loaded = ctx.appconfig.path_loaded
    return loaded.parent if loaded is not None else Path.cwd()


__all__: list[str] = ["DbPlugin"]
