# 节点 Web UI（webui）

节点侧 Web 前端底座：挂载 Vue 构建产物与插件页面，向所有插件提供
`ctx.webui.register_ui` 注册入口（页面 / 插槽 / JS 扩展）。

## 功能

- **SPA 宿主**：`/` 提供 Vue 单页应用（插件管理、从模块管理等页面），
  `/ui-static` 提供构建产物；其中「插件管理」页是 SPA **自带页面**，数据来自
  `plugin-manager` 的 `/api/plugins*` 管理 API（plugin-manager 自身不带 UI）；
- **插件 UI 托管**：插件注册的 Vue 页面 / 组件由本插件统一挂载到
  `/plugin-ui/<owner>/<id>`，随插件卸载自动移除；
- **多后端**：页面可切换到其他 AETP 节点的后端（跨源时需目标节点允许 CORS）。

## 能力接口

### 服务：`ctx.webui`（`provide = "webui"`）

| 方法 | 说明 |
|---|---|
| `register_ui(id, kind="page", format="vue", resource_root, entry, path, title, ...)` | 登记插件 UI 贡献；`kind` 支持 `page` / `slot` / `js` |
| `ui_contributions()` | 列出当前全部 UI 贡献 |

`register_ui` 的生命周期绑定调用方 fiber：插件卸载时静态路由与贡献一并
清理，不需要手动注销。

### HTTP

| 路径 | 说明 |
|---|---|
| `/` | SPA 入口（未匹配路径回退到 SPA） |
| `/ui-static/*` | Vue 构建产物 |
| `/plugin-ui/<owner>/<id>/*` | 插件 UI 静态资源 |

## 使用方法

插件声明 `inject = ["webui"]` 后注册页面：

```python
class MyPlugin:
    name = "my"
    inject = ["webapi", "webui"]

    def __init__(self, ctx, config):
        ctx.webui.register_ui(
            "home",
            kind="page",
            format="vue",
            resource_root=config["resource_root"],
            entry="Home.vue",
            path="/plugins/my",
            title="我的页面",
            menu_group="业务",
            menu_icon="pi pi-star",
            menu_order=10,
        )
```

前端源码在 `frontend/`（Vue 3 + PrimeVue + Vite），`npm run build` 产物
落到 `static/ui/dist`。

## 侧边栏菜单

页面在侧边栏的呈现由 `register_ui` 的三个菜单参数决定：`menu_group` 分组
（不填并入 AETP 组）、`menu_icon` 图标、`menu_order` **菜单权重**（越小越靠前）。
组内与分组间均按权重排序；宿主自带的「插件管理」页权重默认 **100**（即最低，
排最后）。**整个菜单权重最靠前的条目即默认首页**（`/` 重定向目标）；没有插件
页面时菜单里只有「插件管理」，首页即插件管理页。
