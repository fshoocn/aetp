"""webui 插件：可拆卸的 UI 宿主层（SPA 静态资源、页面回退、插件 UI 注册）。

使用方式（在 ``master/main.py`` 里）::

    from masterplugins.webui import WebUiPlugin

    await ctx.plugin(WebUiPlugin)      # 依赖 webapi，需先加载 WebApiPlugin

职责：

1. 挂载 ``/ui-static`` 提供 Vue 构建产物；
2. 设置 SPA 深层 URL 回退（页面导航回 ``index.html``，API/静态仍 404）；
3. 提供 ``ctx.webui.register_ui(...)``：登记插件页面 / 插槽 / JS 扩展，
   并把资源目录挂到 ``/plugin-ui/{owner}/{id}/``；
4. 提供 ``GET /api/web/ui`` 清单接口，供 SPA 启动时构建路由、菜单和插槽。

**为什么 UI 是独立插件**：不加载本插件即为纯 API 部署（无头模式）；
业务 API 路由仍在 ``ctx.webapi`` 注册，API 插件不必依赖 UI 层。
卸载本插件会自动回收静态挂载、SPA 回退和 ``/api/web/ui`` 清单。

UI contribution 的注册需要「谁在注册」（调用方 fiber）来绑定生命周期 ——
与 webapi 的路由注册同理，这依赖 provide 载体上的影子上下文，因此
``register_ui`` 实现为本插件 Service（``provide = "webui"``）的方法。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar, cast

from cordis_port import Context, Fiber, Service
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from .config import WebUiConfig
from .types import UiContribution, UiFormat, UiKind


class WebUiPlugin(Service[WebUiConfig]):
    """UI 宿主插件（Service 类自己就是插件，见开发文档 13.1 的惯用法）。

    业务插件用法::

        class MyPlugin:
            name = "my"
            inject = ["webapi", "webui"]          # 两个都要

            def __init__(self, ctx, config):
                ctx.webui.register_ui(
                    "page", kind="page", format="vue",
                    resource_root=".../ui", entry="Page.vue",
                    path="/my", title="我的页面",
                )
    """

    #: 服务名，带 UI 的插件用 ``inject = ["webapi", "webui"]`` 依赖它
    provide: str | None = "webui"
    #: 插件名
    name: str = "webui"
    #: 依赖 API 服务：UI 是叠在 API 之上的可拆卸上层。
    #: 注意：inject 是给 cordis 读的类级元数据（实例化前就解析依赖），
    #: 必须留在类上 —— 用 ClassVar 标注，不能移进 __init__。
    inject: ClassVar[list[str]] = ["webapi"]
    #: 配置模型，供 cordis 校验 ``ctx.plugin(..., {...})`` 传入的配置
    Config: type[WebUiConfig] = WebUiConfig

    def __init__(self, ctx: Context, config: WebUiConfig | None = None) -> None:
        # ⚠️ 必须最先调用：这一步完成 ctx.webui 的注册
        super().__init__(ctx)
        self.ctx: Context = ctx
        self._provided_ctx: Context = ctx
        self.config: WebUiConfig = (
            config if isinstance(config, WebUiConfig) else WebUiConfig()
        )
        self._ui: dict[str, UiContribution] = {}

    # ------------------------------------------------------------ 生命周期
    def init(self) -> object:
        """webapi 就绪后挂载 UI 静态资源、SPA 回退和清单接口。

        返回值即清理函数：卸载时撤销 SPA 回退（静态挂载与清单路由由
        fiber effect 随本插件自动回收）。
        """
        api = self.ctx.webapi

        api.get("/api/web/ui", self._manifest, kind="api", name="web.ui")

        ui_directory = Path(self.config.ui_directory).expanduser().resolve()
        if ui_directory.is_dir():
            api.static("/ui-static", ui_directory, name="web.ui-static")
        else:
            self.ctx.logger.warning(
                "Vue 构建目录不存在：%s（在 masterplugins/webui/frontend 运行 npm run build 生成）",
                ui_directory,
            )

        api.set_fallback(self._spa_fallback)
        self.ctx.logger.info("webui 已加载：%s", ui_directory)

        def cleanup() -> None:
            api.set_fallback(None)

        return cleanup

    # ------------------------------------------------------ UI contribution
    def register_ui(
        self,
        contribution_id: str,
        *,
        kind: UiKind,
        format: UiFormat,
        resource_root: str | Path,
        entry: str,
        path: str | None = None,
        title: str | None = None,
        menu_group: str | None = None,
        menu_icon: str | None = None,
        menu_order: float = 0,
        target: str | None = None,
        order: float = 0,
    ) -> UiContribution:
        """登记插件 UI contribution 和显式配置的静态资源根目录。

        注册项绑定到**调用方插件的 fiber**：插件卸载时清单条目和
        ``/plugin-ui/...`` 静态挂载自动移除。
        """
        if not contribution_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", contribution_id):
            raise ValueError("contribution_id 只能包含字母、数字、点、下划线和连字符")
        accepted_formats = {
            "page": {"vue"},
            "slot": {"vue", "html"},
            "script": {"js"},
        }
        if kind not in accepted_formats or format not in accepted_formats[kind]:
            raise ValueError(f"不支持的 UI 类型与资源格式组合：{kind!r}/{format!r}")
        if kind == "page" and (not path or not title):
            raise ValueError("page contribution 必须提供 path 和 title")
        if kind == "slot" and not target:
            raise ValueError("slot contribution 必须提供 target")
        if not isinstance(cast(object, entry), str) or not entry.strip():
            raise ValueError("entry 必须是 resource_root 下的相对文件路径")

        entry_path = Path(entry.replace("\\", "/"))
        if entry_path.is_absolute() or any(part in {"", ".", ".."} for part in entry_path.parts):
            raise ValueError("entry 必须是 resource_root 下不含路径穿越的相对路径")
        allowed_suffixes: tuple[str, ...]
        if format == "vue":
            allowed_suffixes = (".vue",)
        elif format == "html":
            allowed_suffixes = (".html",)
        else:
            allowed_suffixes = (".js", ".mjs")
        if entry_path.suffix.lower() not in allowed_suffixes:
            raise ValueError(f"{format} contribution 的 entry 扩展名不正确")

        root = Path(resource_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"插件 UI 资源目录不存在：{root}")
        resolved_entry = (root / entry_path).resolve()
        if not resolved_entry.is_relative_to(root) or not resolved_entry.is_file():
            raise ValueError(f"插件 UI 入口文件不存在或超出资源目录：{entry}")

        caller_fiber = self._caller_fiber()
        owner = _fiber_name(caller_fiber)
        owner_slug = _owner_slug(owner)
        full_id = f"{owner}:{contribution_id}"
        if full_id in self._ui:
            raise ValueError(f"UI contribution 已注册：{full_id}")

        static_path = f"/plugin-ui/{owner_slug}/{contribution_id}"
        api = self.ctx.webapi
        api.static(static_path, root)

        item = UiContribution(
            id=full_id,
            kind=kind,
            format=format,
            owner=owner,
            entry=f"{static_path}/{entry_path.as_posix()}",
            path=_normalize_path(path) if path else None,
            title=title,
            menu_group=menu_group,
            menu_icon=menu_icon,
            menu_order=menu_order,
            target=target,
            order=order,
        )
        self._ui[full_id] = item

        def cleanup() -> None:
            self._ui.pop(full_id, None)
            api.remove(static_path)

        caller_fiber.effect(lambda: cleanup, f"ctx.webui.register_ui({full_id!r})")
        return item

    def ui_contributions(self) -> list[UiContribution]:
        """返回按类型、排序和 ID 稳定排序的前端扩展清单。"""
        return sorted(
            self._ui.values(),
            key=lambda item: (item.kind, item.menu_order, item.order, item.title or item.id),
        )

    # -------------------------------------------------------------- 内部实现
    def _manifest(self, _request: Request) -> list[dict[str, str | float | None]]:
        """``GET /api/web/ui``：Vue 宿主启动时拉取的插件 UI 清单。"""
        return [item.to_dict() for item in self.ui_contributions()]

    def _spa_fallback(self, request: Request) -> Response | None:
        """SPA 深层 URL 回退；API 与静态前缀、非 GET/HEAD 一律不接管（返回 ``None``）。"""
        path = request.url.path
        if path == "/api" or path.startswith("/api/"):
            return None
        if path == "/ui-static" or path.startswith("/ui-static/"):
            return None
        if path == "/plugin-ui" or path.startswith("/plugin-ui/"):
            return None
        if request.method not in {"GET", "HEAD"}:
            return None

        ui_index = Path(self.config.ui_directory).expanduser().resolve() / "index.html"
        if not ui_index.is_file():
            return JSONResponse(
                {
                    "detail": "Vue UI is not built; run npm run build in plugins/webui/frontend"
                },
                status_code=503,
            )
        if request.method == "HEAD":
            return Response(
                status_code=200,
                media_type="text/html",
                headers={"Content-Length": str(ui_index.stat().st_size)},
            )
        return FileResponse(ui_index)

    def _caller_ctx(self) -> Context:
        """取本次调用的「使用点」上下文（即调用方插件自己的 ctx）。

        与 webapi 同理：服务方法内的 ``self.ctx`` 是 cordis 的影子上下文，
        ``.fiber`` 即调用方 fiber。直接调用（不经过代理）时退回提供方自身。
        """
        return self.ctx

    def _caller_fiber(self) -> Fiber:
        """取调用方插件的 fiber，用于把 contribution 绑定到它的生命周期上。"""
        ctx = self._caller_ctx()
        fiber = getattr(ctx, "fiber", None)
        return fiber if fiber is not None else self._provided_ctx.fiber


def _fiber_name(fiber: Fiber | None) -> str:
    """取 fiber 名用于诊断（``UiContribution.owner``）；无 fiber 时回退 ``"root"``。"""
    return getattr(fiber, "name", None) or "root"


def _owner_slug(owner: str) -> str:
    """把插件名转换为 URL path segment。"""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", owner).strip("-") or "plugin"


def _normalize_path(path: str) -> str:
    """规范化前端路由路径（保证以 ``/`` 开头）。"""
    path = path.strip()
    return path if path.startswith("/") else f"/{path}"


__all__: list[str] = ["WebUiPlugin"]
