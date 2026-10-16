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
| `get/post/delete(path, kind="api", name=...)` | 登记 HTTP 路由（handler 同步 / 异步均可） |
| `static(prefix, root, name=...)` | 挂载静态目录 |
| `remove(name)` | 按登记名移除路由 |
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
