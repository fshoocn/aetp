"""appconfig 插件入口：把 ``config.ini`` 注册为 ``ctx.appconfig`` 服务。

加载后其他插件声明 ``inject = ["appconfig"]`` 即可取用配置数据::

    class MyPlugin:
        inject = ["appconfig"]

        def __init__(self, ctx, config):
            port = ctx.appconfig.get_int("web", "port", 8080)
            name = ctx.appconfig.section("node").name
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from cordis_port import Context, Service

from .config import AppConfig, ConfigSection


class AppConfigPlugin(Service[dict[str, object]]):
    """应用配置插件（``ctx.appconfig`` 服务的载体）。

    启动时读取 ``path`` 指向的 ini 文件（默认 ``config.ini``，相对路径按
    当前工作目录即执行根目录解析）。**文件不存在时按空配置启动并告警**，
    不阻断节点启动；文件存在但不是合法 ini 时启动失败（快速暴露错误）。
    """

    #: 服务名：其他插件 ``inject = ["appconfig"]`` 后经 ``ctx.appconfig`` 取配置
    provide: str | None = "appconfig"
    #: 插件名
    name: str = "appconfig"
    #: 配置是最早就绪的基础服务，不依赖其他服务
    inject: ClassVar[list[str]] = []

    def __init__(self, ctx: Context, config: dict[str, object] | None = None) -> None:
        # ⚠️ 必须最先调用：这一步完成 ctx.appconfig 的注册
        super().__init__(ctx)
        # 服务方法经代理调用时 self.ctx 是调用方影子 ctx，内部机件用自留的真实 ctx
        self._own_ctx: Context = ctx
        raw = config if isinstance(config, dict) else {}
        self.path: Path = Path(str(raw.get("path", "config.ini"))).expanduser()
        self._config: AppConfig = AppConfig()

    def init(self) -> None:
        """解析 ini 文件（失败快速暴露，缺失则降级空配置）。"""
        resolved = self.path.resolve()
        if not resolved.is_file():
            self._own_ctx.logger.warning(
                "应用配置文件不存在：%s（按空配置启动）", resolved
            )
            return
        self._config = AppConfig.from_file(resolved)
        self._own_ctx.logger.info(
            "应用配置已加载：%s（%d 个节）", resolved, len(self._config.sections())
        )

    # ------------------------------------------------- ctx.appconfig 服务 API
    @property
    def path_loaded(self) -> Path | None:
        """实际加载的 ini 文件路径；未加载返回 ``None``。"""
        return self._config.path

    def section(self, name: str) -> ConfigSection:
        """取配置节视图（不存在返回空节）。"""
        return self._config.section(name)

    def sections(self) -> list[str]:
        """全部节名。"""
        return self._config.sections()

    def get(self, section: str, key: str, fallback: str | None = None) -> str | None:
        """按键取字符串值；节或键不存在返回 ``fallback``。"""
        return self._config.get(section, key, fallback)

    def get_int(
        self, section: str, key: str, fallback: int | None = None
    ) -> int | None:
        """按键取整数值；值不是整数抛 :class:`ValueError`。"""
        return self._config.get_int(section, key, fallback)

    def get_float(
        self, section: str, key: str, fallback: float | None = None
    ) -> float | None:
        """按键取浮点值；值不是数字抛 :class:`ValueError`。"""
        return self._config.get_float(section, key, fallback)

    def get_bool(
        self, section: str, key: str, fallback: bool | None = None
    ) -> bool | None:
        """按键取布尔值（``1/yes/true/on`` 与 ``0/no/false/off``）。"""
        return self._config.get_bool(section, key, fallback)

    def to_dict(self) -> dict[str, dict[str, str]]:
        """导出为 ``{节: {键: 值}}``。"""
        return self._config.to_dict()


__all__: list[str] = ["AppConfigPlugin"]
