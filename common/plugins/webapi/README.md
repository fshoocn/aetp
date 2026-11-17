# Web API 服务包（webapi / plugin-manager）

节点 Web 底座：HTTP API、路由与静态资源（不含 UI 宿主）。随平台直装，
主从共用；`plugin-manager`（插件管理）与本包同目录，一并说明。

## 功能

- **Web 服务生命周期**：按 `[web]` 节配置启动 uvicorn + Starlette，
  `await` 返回时端口已就绪；`[web]` 节值由 appconfig 的 `internal/config`
  钩子注入为默认值（`WebApiConfig.ini_section = "web"`），显式传入 /
  `fiber.update({...})` 覆盖它，提交时经 `internal/commit` 写回该节；
- **路由表**：`ctx.webapi.get/post/delete/static` 登记路由，随插件卸载
  自动清理；
- **插件管理**（plugin-manager）：插件包安装 / 启停 / 卸载 / 覆盖升级 /
  归档回溯，暴露 `ctx.plugins` 服务与 `/api/plugins` 管理 API。**本插件
  不带 UI**——「插件管理」页面是 `webui` 插件的 SPA 自带页面（`/plugins`），
  调用这里的管理 API；不加载 webui 即纯 API（无头）部署。

## 能力接口

### 服务：`ctx.webapi`（`provide = "webapi"`）

| 方法 | 说明 |
|---|---|
| `get/post/put/patch/delete(path, kind="api", name=...)` | 登记 HTTP 路由（handler 同步 / 异步均可；GET 自动携带 HEAD） |
| `route(path, methods, handler, kind=..., name=...)` | 显式指定方法集登记 |
| `api(path, handler, name=...)` | 便捷登记（api 前缀路由的统一入口） |
| `static(path, directory, *, html=False, name=...)` | 挂载静态目录（`html=True` 时目录内 index.html 作首页） |
| `remove(path)` | 移除该路径下的**全部**路由（含静态挂载）——按路径前缀删除，不是按登记名 |
| `use(fn, phase, order, on_error, kinds)` | 登记拦截链 hook（见下节）；绑调用方 fiber，卸载自动摘除 |
| `mounts()` | 挂载资源清单：`endpoints`（路由记录：path 模板 / methods / kind / name / owner）+ `pages`（UI 页面贡献：SPA 路径 / 标题 / 归属）——权限按「方法 + 链接」精细匹配的规则来源 |
| `pipeline.describe()` / `pipeline.stats()` | 拦截链自省：逐 hook 的阶段 / 顺序 / 失败策略与调用 / 出错 / 自身耗时 |
| `address()` / `is_running()` | 当前服务地址 / 就绪状态 |

### 服务：`ctx.plugins`（`provide = "plugins"`）

| 方法 | 说明 |
|---|---|
| `list_plugins()` | 列出已安装插件（含内置 / 预装标记） |
| `install_archive(bytes, replace=False)` | 安装插件包；同 id 需 `replace=True` 显式确认 |
| `enable(id)` / `disable(id)` / `uninstall(id)` | 启停 / 卸载（内置与预装插件受保护） |
| `update_config(id, config)` | 部分更新插件配置（运行中热更新并写回 `config.ini`，未启用只存待生效） |
| `plugin_readme(id)` | 读取插件描述文件（README.md） |

## 使用方法

插件声明 `inject = ["webapi"]`（需要安装能力则再加 `"plugins"`）：

```python
class MyPlugin:
    inject = ["webapi"]

    def init(self):
        @self.ctx.webapi.get("/api/my", kind="api")
        def handler(request):
            return {"ok": True}
```

⚠️ 内置插件（`appconfig` / `webapi` / `plugin-manager`）随平台直装，
**不可安装 / 升级 / 启停 / 卸载 / 配置修改**（插件管理会拒绝同 id 的上传安装，
防止与直装服务同名冲突）。预装插件（config.ini `[node] plugins` 列出的
插件源，如 `webui`）不锁定：停用 / 卸载后重启时 `ensure_installed` 会
自动重装重置。

## 拦截链（hook 点）：拦请求 / 改请求 / 观测

拦截器签名 `async (rc: RequestContext, nxt) -> Response`（洋葱模型，参照
Spring 的 `HandlerInterceptor` / Spring Security 过滤器链语义）：

* **短路**＝不调 `next()` 并自行返回（如权限拒绝 403）；
* **改响应**＝替换 `await nxt()` 的返回值（对应 `postHandle` / `ResponseBodyAdvice`）；
* **改请求**＝ `rc.enhance(headers=..., query=..., body=...)`（body ≤ 64 KiB，
  须在 handler 读取前调用；响应发完后另有事件，见下）；
* **测量 / 清理**＝ `try/finally` 包 `next()`（对应 `afterCompletion`）。

**阶段与建议 order 分段**（顺序由 phase + order 决定，与插件加载顺序无关）：

| 阶段 | order 段 | 典型角色 | on_error |
|---|---|---|---|
| `observe` | 10–90 | 追踪、日志、耗时观测 | SKIP（fail-open） |
| `guard` | 100–199 | 限流、CSRF、防重放 | FAIL |
| `authorize` | 200–299 | 权限合规检查 | FAIL（fail-closed） |
| `inner` | 300+ | 业务增强、响应包装 | 视语义 |

菜谱——**权限合规插件**（不合规即拒，检查器自身故障也不放行）：

```python
async def check(rc: RequestContext, nxt: WebNext) -> Response:
    if not allowed(rc.identity, rc.request.url.path, rc.request.method):
        return JSONResponse({"error": "forbidden"}, status_code=403)  # 短路
    return await nxt()

ctx.webapi.use(check, phase="authorize", order=200, on_error=OnError.FAIL)
```

菜谱——**API 热点统计插件**（纯观测，不进热路径洋葱）：

```python
ctx.on("webapi/request-completed", record)   # 精确时延（含流式 / 静态 / 404）
```

### `webapi/request-completed` 事件（parallel）

响应**真正发完**后派发（ASGI 栈最外层观察者；401 / 404 / 静态资源同样覆盖）：

| 字段 | 说明 |
|---|---|
| `path` / `method` | 请求链接与方法 |
| `status` | 响应状态码（未发出为 0） |
| `duration_ms` | 端到端服务侧时延（到响应发完，含流式） |
| `bytes` | 响应体累计字节 |
| `aborted` | 响应是否未正常发完（断连 / 取消 / 未捕获异常时置位；best-effort，不读 receive 的 handler 观察不到纯断连） |
| `route` | 命中的路由名（无匹配为空串） |
| `identity` | 身份（`request.state.auth`，未认证为 `None`） |

计时口径与拦截链的 `stats()`（单 hook 自身耗时）互补：请求级指标认本事件。

### 权限匹配的数据口径

权限规则的**唯一权威来源是 `ctx.webapi.mounts()`**（同 `GET /api/web/mounts`）：
`endpoints`（接口：path 模板 / methods / name / owner）+ `pages`（页面：SPA
路径 / title / owner，经 `common.contracts.UiCatalogInterface` 契约汇集，
webapi 不依赖 webui 包）。`/api/web/routes`（全路由诊断）、`/api/web/ui`
（前端运行时清单）只作调试视图，勿作规则来源；静态挂载缺省名
`static:<path>`，按 name 匹配不留盲区。
