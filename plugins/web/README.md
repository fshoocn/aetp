# Web 插件

`plugins/web` 提供 AETP 的 Vue UI 宿主、HTTP API、静态资源和插件 UI contribution 注册。所有 UI 页面由 `frontend/` 下的 Sakai Vue SPA 渲染；Python 不再生成 UI HTML 页面。

## 启动与构建

```powershell
Push-Location plugins/web/frontend
npm ci
npm run build
Pop-Location
python master/main.py
```

Vite 将产物生成到 `plugins/web/static/ui/dist/`，Web 插件从 `/ui-static/` 提供静态文件。Vue History 深层 URL 未匹配时回退到 SPA `index.html`；`/api/*`、`/ui-static/*` 和 `/plugin-ui/*` 不参与回退。Python 保留 API 和静态文件服务，不保留旧的 `page()`/HTML layout。

主入口只需加载 `WebPlugin`；它会自动注册插件管理 API，并在启动时恢复已启用插件。左侧“插件管理”页面提供 ZIP 上传、启用、停用和卸载。默认安装目录为 `plugins/installed/`，可通过 Web 配置项 `plugin_install_root` 修改；该目录是运行时数据，不应提交到 Git。

管理 API：

| 方法与路径 | 操作 |
|---|---|
| `GET /api/plugins` | 获取已安装插件及 enabled 状态 |
| `POST /api/plugins/upload` | 原始 `application/zip` 请求体上传插件包 |
| `POST /api/plugins/{id}/enable` | 导入并启用插件入口类 |
| `POST /api/plugins/{id}/disable` | dispose 插件 Fiber 并停用 |
| `DELETE /api/plugins/{id}` | 停用并删除插件包 |

插件包 ZIP 根目录必须包含 `plugin.json` 和 Python 入口文件。最小清单：

```json
{
    "id": "example",
    "name": "示例插件",
    "version": "1.0.0",
    "entrypoint": "plugin:ExamplePlugin",
    "resource_root": "ui",
    "config": {}
}
```

`entrypoint` 是 `module:ClassName`；`resource_root` 是 ZIP 内相对目录。若插件提供 UI，其 `ui/` 目录中放置 `.vue`、HTML 和 JS 入口，Python 插件通过 `ctx.web.register_ui(...)` 声明页面/插槽。上传后状态为停用，用户点击启用；安装、启停或卸载后刷新浏览器以更新菜单。ZIP 最大 32 MiB，解压后最大 128 MiB，禁止绝对路径、`..` 路径和符号链接。

## 业务 API

业务插件继续通过 `ctx.web` 注册 JSON、通用路由和静态资源。声明 `inject = ["web"]` 后，Web 服务在插件构造前已就绪。

```python
class ExamplePlugin:
    name = "example"
    inject = ["web"]

    def __init__(self, ctx, config):
        self.ctx = ctx

        @ctx.web.get("/api/example/status", kind="api")
        def status(request):
            return {"ready": True}
```

插件卸载会自动注销其路由和 UI contribution。

## 插件 UI 资源

每个插件显式配置 `resource_root`，指向插件自己的 UI 资源目录，入口路径相对该目录。注册时后端验证目录和入口文件，并挂载到 `/plugin-ui/{owner}/{contribution-id}/`。

```text
plugins/example/
  plugin.py
  ui/
    Dashboard.vue
    StatusCard.vue
    header.html
    extension.js
```

Contribution 的 ID 在单个插件内唯一；资源目录必须存在，入口必须处于该目录内。支持格式：`page/vue`、`slot/vue`、`slot/html`、`script/js`。

注意：`/plugin-ui/{owner}/{contribution-id}/` 是资源目录前缀，不是页面地址；目录本身没有 `index.html`，直接打开会返回 404。`page` 请打开 contribution 的 `path`（例如 `/plugins/overview`）；`slot` 组件只会在 `target` 对应的 Sakai 页面内显示，不能当独立页面访问。

## 独立 Vue 页面

页面注册会加入 Sakai Vue Router 和左侧菜单。插件页面组件运行在宿主 Vue App 中，共享 Aura 主题、PrimeVue 配置和布局。

```python
ctx.web.register_ui(
    "dashboard",
    kind="page",
    format="vue",
    resource_root=config["resource_root"],
    entry="Dashboard.vue",
    path="/plugins/example",
    title="示例插件",
    menu_group="业务插件",
    menu_icon="pi pi-fw pi-box",
    menu_order=20,
)
```

## Vue / HTML 命名插槽

宿主目前提供这些 outlet：

| Outlet | 位置 |
|---|---|
| `app:header-actions` | 全局顶栏操作区 |
| `dashboard:top` | 仪表盘内容顶部 |
| `dashboard:widgets` | 仪表盘 widget 区 |

```python
ctx.web.register_ui(
    "status-card",
    kind="slot",
    format="vue",
    resource_root=config["resource_root"],
    entry="StatusCard.vue",
    target="dashboard:widgets",
    order=10,
)

ctx.web.register_ui(
    "notice",
    kind="slot",
    format="html",
    resource_root=config["resource_root"],
    entry="header.html",
    target="app:header-actions",
    order=20,
)
```

Vue SFC 使用 `vue3-sfc-loader` 按需编译，loader 复用宿主 Vue runtime。HTML 文件作为局部片段插入 outlet，不应带完整 `html/head/body` 文档壳。插件样式应使用 scoped CSS、宿主语义变量和共享 PrimeVue 主题。

## JS Extension

JS 入口是同源 ESM，并导出 `activate(context)`。context 提供 API fetch、Vue Router 以及命名 outlet API。`mountHtml`/`mountVue` 返回单项清理函数；`activate` 返回的函数在插件 runtime 清理时调用。

```js
export function activate(context) {
    const removeNotice = context.slots.mountHtml(
        'dashboard:top',
        '<div class="example-notice">插件已连接</div>'
    );
    const removeStatus = context.slots.mountVue(
        'dashboard:widgets',
        new URL('./StatusCard.vue', import.meta.url).href
    );

    return () => {
        removeNotice();
        removeStatus();
    };
}
```

```python
ctx.web.register_ui(
    "extension",
    kind="script",
    format="js",
    resource_root=config["resource_root"],
    entry="extension.js",
)
```

插件脚本按可信代码运行在宿主页面上下文中。JS extension 创建的订阅、定时器和 DOM 外部资源必须在 dispose 回调中自行清理。

## 清单与生命周期

- `GET /api/web/ui` 返回所有已注册 contribution，供 SPA 启动时构建插件路由、菜单和插槽。
- 插件安装、卸载或配置变化后需要用户刷新页面；首版不做 SSE/WebSocket 热更新。
- 插件 fiber 卸载时后端清除 UI 清单和插件静态挂载；前端清理 JS 扩展、路由和动态样式。
- `GET /api/web/routes` 返回已注册 API/通用路由和静态挂载，便于诊断。

## Web 配置

```python
await ctx.plugin(WebPlugin, {
    "host": "127.0.0.1",
    "port": 8080,
    "ui_directory": "plugins/web/static/ui/dist",
    "access_log": False,
    "log_level": "warning",
    "start_timeout": 10.0,
})
```

若从仓库根目录启动，`ui_directory` 使用默认值即可。`npm run build` 生成该目录。