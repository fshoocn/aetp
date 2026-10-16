"""appconfig 插件入口：把 ``config.ini`` 注册为 ``ctx.appconfig`` 服务。

加载后其他插件声明 ``inject = ["appconfig"]`` 即可取用配置数据::

    class MyPlugin:
        inject = ["appconfig"]

        def __init__(self, ctx, config):
            port = ctx.appconfig.get_int("web", "port", 8080)
            name = ctx.appconfig.section("node").name

同时注册两个配置生命周期钩子（依赖 :class:`common.config_base.FieldConfig`
的 ``ini_section`` 声明）：

* ``internal/config`` —— 插件配置解析前，把对应 ini 节的值注入为**默认值层**
  （显式传入 > ini 节值 > 类属性默认值）；
* ``internal/commit`` —— ``fiber.update(...)`` 提交时，把新配置经
  ``to_ini`` 序列化写回该节（``no_save=True`` 跳过）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar, cast

from cordis_port import Context, Fiber, Service

from common.config_base import FieldConfig

from .config import AppConfig, ConfigSection


class AppConfigPlugin(Service[dict[str, object]]):
    """应用配置插件（``ctx.appconfig`` 服务的载体）。

    启动时读取 ``path`` 指向的 ini 文件（默认 ``config.ini``，相对路径按
    当前工作目录即执行根目录解析）。**文件不存在时按空配置启动并告警**，
    不阻断节点启动；文件存在但不是合法 ini 时启动失败（快速暴露错误）。
    """

    #: 服务名：其他插件 ``inject = ["appconfig"]`` 后经 ``ctx.appconfig`` 取配置
    provide: str | None = "appconfig"
    #: 插件名（即插件 id）
    name: str = "appconfig"
    #: 展示名与版本：内置插件没有 plugin.json，清单元数据由插件类属性提供
    display_name: ClassVar[str] = "应用配置"
    version: ClassVar[str] = "1.0.0"
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
        #: 实际读 / 写的 ini 文件（含文件缺失时的预期写入位置）
        self._save_path: Path = self.path

    def init(self) -> None:
        """解析 ini 文件（失败快速暴露，缺失则降级空配置）并注册配置钩子。"""
        resolved = self.path.resolve()
        self._save_path = resolved
        if not resolved.is_file():
            self._own_ctx.logger.warning(
                "应用配置文件不存在：%s（按空配置启动）", resolved
            )
        else:
            self._config = AppConfig.from_file(resolved)
            self._own_ctx.logger.info(
                "应用配置已加载：%s（%d 个节）", resolved, len(self._config.sections())
            )
        # 配置生命周期钩子：注入默认值（解析前）与写回（提交时）
        self._own_ctx.on("internal/config", self._inject_defaults)
        self._own_ctx.on("internal/commit", self._persist_config)

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

    def update_section(self, name: str, values: Mapping[str, str]) -> None:
        """更新配置节键值并立即写回 ``config.ini``（保留注释，见
        :meth:`AppConfig.update_section`）。
        """
        self._config.update_section(name, values, path=self._save_path)
        self._own_ctx.logger.info(
            "配置已写回 %s 的 [%s] 节（%s）",
            self._save_path,
            name,
            ", ".join(sorted(values)),
        )

    # ------------------------------------------------- 配置生命周期钩子
    def _inject_defaults(
        self, fiber: Fiber, next_hook: Callable[[], object]
    ) -> object:
        """``internal/config`` 监听：把 ``[ini_section]`` 节值注入为默认值层。

        显式传入的配置覆盖 ini 节值，ini 节值覆盖配置类属性默认值；
        本监听器在 Schema 校验**之前**改写，注入的值同样受类型校验保护。
        未声明 ``ini_section`` 的插件原样放行。
        """
        raw = next_hook()
        schema = _config_schema(fiber)
        if schema is None or schema.ini_section is None:
            return raw
        defaults = schema.coerce_ini(
            self._config.section(schema.ini_section).to_dict()
        )
        if raw is None:
            return dict(defaults)
        overlay: dict[str, object] = {}
        if isinstance(raw, Mapping):
            overlay.update(cast(Mapping[str, object], raw))
        elif isinstance(raw, schema):
            overlay.update(raw.to_dict())
        else:
            return raw
        return {**defaults, **overlay}

    def _persist_config(self, fiber: Fiber, config: object) -> None:
        """``internal/commit`` 监听：``fiber.update()`` 提交时写回 ini 节。

        只写**实际提交**的键（部分更新 = 部分写回）；
        ``update(..., no_save=True)`` 不触发本事件，天然跳过写回。
        未声明 ``ini_section`` 的插件无持久化落点，静默跳过。
        """
        schema = _config_schema(fiber)
        if schema is None or schema.ini_section is None:
            return
        values = schema.to_ini(config)
        if not values:
            return
        self._config.update_section(schema.ini_section, values, path=self._save_path)
        self._own_ctx.logger.info(
            "插件配置已写回 %s 的 [%s] 节（%s）",
            self._save_path,
            schema.ini_section,
            ", ".join(sorted(values)),
        )


def _config_schema(fiber: Fiber) -> type[FieldConfig] | None:
    """取 fiber 对应插件声明的 ``Config`` schema（须是 ``FieldConfig`` 子类）。"""
    runtime = fiber.runtime
    schema = runtime.Config if runtime is not None else None
    if isinstance(schema, type) and issubclass(schema, FieldConfig):
        return cast(type[FieldConfig], schema)
    return None


__all__: list[str] = ["AppConfigPlugin"]
