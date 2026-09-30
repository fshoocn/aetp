# Vue 前端与插件 UI 集成方案

> 状态：首版 MVP 已落地，稳定化事项待完成。  
> 日期：2026-09-29  
> 目标：基于 `D:\sakai-vue` 的 Sakai Vue 模板构建平台 UI；允许可信插件注册独立页面，或通过宿主 JS 在其他页面插入 HTML、Vue 组件和插件脚本。

## 1. 结论摘要

整体可行，建议采用“**Sakai 构建后的 SPA 作为主 UI，插件 UI 通过宿主扩展运行时接入**”的混合方案：

- Sakai 主界面仍通过 Vite 正常构建，不在生产浏览器里动态编译整套管理后台。
- 可信插件可以注册 Vue Router 页面，也可以向宿主页面的命名插槽挂载 HTML 或 Vue 组件，还可以通过 JS 模块执行带清理回调的扩展逻辑。
- 插件 UI 统一通过宿主清单和前端挂载器发现；菜单、路由、页面插槽和 JS 模块都绑定到插件生命周期。
- 本方案假设所有插件可信，插件脚本运行在宿主页面上下文中；不设计 iframe 沙箱。仍使用清晰的 UI API 和样式约定，避免插件之间意外冲突。

初步判断：主界面可行性高；插件路由、插槽和 JS 注入技术上可行，复杂度中等。关键工作是定义稳定的挂载点、扩展生命周期、页面接入方式和统一样式 API。

## 2. 现状与关键约束

### AETP Web

- `plugins/web` 当前提供 Python 页面布局、路由、静态目录和 API；目标架构改为 Sakai SPA 独占 UI 页面，Python Web 只承载 API 与前端静态产物。
- `RouteRegistry` 把路由按注册顺序追加至 Starlette；静态目录使用 `Mount`，可能匹配其前缀下的所有子路径。
- 当前 404 处理器只渲染欢迎页/404 页，尚未提供 Vue SPA 的 `index.html` 回退。
- 业务插件调用 `ctx.web` 注册的路由和导航会绑定到调用方 fiber，插件卸载时自动清理。
- `ctx.web` 是 cordis `Service` 提供的代理。需要知道注册方 fiber 的新注册 API 必须直接定义在 `WebService` 实例上，不能只包在一个普通 manager/helper 对象里，否则调用方上下文可能丢失。

### Sakai Vue 模板

- `D:\sakai-vue` 是 Vue 3、Vue Router 4、PrimeVue 4、Vite 5 项目；入口通过 `createWebHistory()` 使用 History 路由。
- 模板的自动组件导入、Tailwind 类扫描等是构建期能力，不会自动应用到运行时下载的 SFC。
- 模板附带 MIT 许可证。将模板复制或改造后随项目分发时，应保留其许可证文本。

### `vue3-sfc-loader`

- 官方 README 描述其可在浏览器中加载、编译 `.vue`，支持递归依赖、样式注入和自定义 `getFile`、`moduleCache` 等钩子，不要求插件 SFC 再走 Node 构建。
- GitHub 当前显示版本为 `0.9.5`，最近发布/维护活动较久；应固定版本并先做兼容性 PoC。
- loader 负责 SFC 编译，不负责应用的网络策略、插件授权或安全隔离；这些需要宿主实现。

## 3. 可行性与难点

| 能力 | 判断 | 主要难点/约束 |
|---|---|---|
| Sakai 作为平台主界面 | 高 | 把模板纳入仓库并稳定构建；迁移期间明确 SPA 页面 URL 与保留 API URL，切换完成后不再保留 Python HTML UI。 |
| Vue Router 深层 URL 刷新 | 高，需后端配合 | `/dashboard` 等前端路由刷新时，后端要在未命中时返回 SPA `index.html`；不能用先注册的根目录 catch-all 静态 Mount，否则可能截获 `/api`。 |
| 插件 `.vue` 动态加载 | 中 | loader 的 `getFile`、相对路径解析、缓存和 CSS 注入都要由宿主提供；动态导入依赖必须受控。 |
| 插件菜单和路由注册 | 中 | 当前 `NavItem` 只有导航信息，没有组件入口；需要增加 UI 贡献契约和清单 API，并绑定到插件 fiber 清理。 |
| 在其他页面注入 HTML/Vue/JS | 中 | 需要宿主侧 JS runtime 和稳定命名插槽；不经统一布局渲染的完整 HTML 页面必须显式加载挂载器。 |
| PrimeVue 组件复用 | 中 | 主应用的 PrimeVue 插件可供同一个 Vue App 下的组件使用，但 loader 不会自动执行 Vite 的组件自动导入。需要提供平台 SDK/全局组件或严格受控的模块映射。 |
| 插件样式统一 | 中 | 需要平台 tokens、共享组件和局部样式规范；运行时注入的样式和组件必须按插件生命周期清理。 |
| 插件安全隔离 | 不在范围内 | 假设插件可信，JS 与 SFC 在主页面上下文运行；插件可以访问宿主页面能力，按产品约定由插件审核和信任机制负责。 |
| 生产维护性 | 中 | loader 发布较久未更新，需评估当前 Vue 3.4/PrimeVue 4 的兼容性、浏览器运行时开销和缓存策略。 |

## 4. 推荐架构

### 4.1 主 UI：Sakai 预构建 SPA

1. 将 `D:\sakai-vue` 作为前端模板来源，纳入 AETP 可复现的仓库目录（建议 `frontend/`），而不是让运行环境依赖 D 盘绝对路径。
2. 在模板中配置 Vite `base`、API 地址约定和生产构建输出目录；版本锁定在 `package-lock.json` 或团队选定的单一 lockfile。
3. 将构建产物部署到 AETP 可定位的目录，例如 `plugins/web/static/ui/dist/`。Node/Vite 用于构建，Python Web 服务只负责提供产物和 API。
4. 为 SPA 页面提供 `/` 入口及受限的 404 回退：仅对前端页面路径返回 `index.html`；`/api/*`、`/assets/*`、缺失静态文件仍返回真实 404。不要注册会遮蔽后续 API 的 `/` 通配 Mount。
5. UI 全部由 Sakai/Vue 路由承载；迁移完成后退役 Python `page` 页面、字符串拼装的 UI 布局和欢迎 HTML。保留 API、静态文件服务及必要的通用 HTTP 响应能力。
6. 保持 API 同源，首版不引入跨域配置。当前 Web 默认监听 `127.0.0.1`；若部署到非本机网络，认证和访问控制仍需另行设计。

### 4.2 插件 UI 贡献模型

建议在 `WebService` 上增加 `register_ui(...)` 一类代理方法，并以单个 UI contribution 表示插件声明的扩展。贡献至少包括：

- 全局唯一的 `id` 和插件 `owner`；
- `kind`：`page`（独立路由）、`slot`（注入宿主页面）或 `script`（JS 扩展模块）；
- 资源入口：SFC 路径、HTML 片段路径或 JS 模块路径；
- 对应类型的元数据：页面 `path/title/menu`，插槽 `target/position/order`，或脚本的激活入口；
- 插件显式提供 `resource_root`（建议来自插件配置，指向该插件的 `ui/` 目录）；贡献中的文件入口均相对于该根目录；
- 可选的前端启用条件（例如只在某个页面装载），由宿主统一判断。

宿主提供 `GET /api/web/ui` 返回 contribution 清单。插件必须显式配置并登记自己的 `resource_root`，不通过扫描默认目录推断；后端把它挂载为插件独立静态前缀，并验证实际文件仍处于该目录中。`register_ui` 必须直接实现于 `WebService`，从调用代理拿到插件 fiber；插件卸载时同时移除清单、静态挂载和资源归属。

### 4.3 三种挂载方式

#### A. 独立 Vue 页面

`page` contribution 指定前端路由、页面组件和菜单元数据。Sakai 启动后读取清单，为视图调用 `router.addRoute()`；组件由 `vue3-sfc-loader` 按需加载。插件页面仍显示在宿主的 Sakai 布局内。

#### B. 在其他页面插入 HTML 或 Vue

主应用在可扩展位置放置宿主 outlet，例如 `app:header-actions`、`dashboard:top`、`dashboard:widgets`。插件以 `slot` contribution 声明目标 outlet、顺序和资源入口，宿主在目标页面挂载时自动装载，在离开页面或插件卸载时自动回收。

- HTML contribution 用宿主 `PluginHtmlOutlet` 将插件片段插入 outlet；片段应是局部内容，不应包含第二套 `html/head/body` 文档壳。HTML 可以放在插件静态目录，也可作为注册数据，但优先用文件以便维护。
- Vue contribution 用 `PluginComponentOutlet` 动态解析 SFC，并作为宿主 Vue 树中的子组件渲染。不要对每个贡献都另建 `createApp()`，否则会失去宿主的 PrimeVue、主题、路由和 provide/inject 上下文。
- 默认只向平台声明的 outlet 挂载，不依赖 `document.querySelector()` 操作 Sakai 私有 DOM。若将来开放任意 CSS selector 挂载，应视为不稳定兼容接口。

#### C. 插件 JS 扩展

`script` contribution 指定同源 ESM 入口。宿主只在目标页面/时机激活该模块，并传入有限且版本化的上下文，例如 `api`、`slots`、`router`、`ui`。模块返回清理函数：

```js
export function activate(context) {
	const stop = context.api.onStatusChanged((status) => {
		context.slots.update('dashboard:widgets', status);
	});

	return () => stop();
}
```

脚本应通过 `context.slots`、`context.ui` 等宿主 API 创建扩展，不直接改写宿主 DOM。激活函数抛错、页面卸载或插件卸载时，宿主调用返回的清理函数；未返回清理函数的脚本仍可执行，但不符合可卸载扩展契约。可信插件可直接访问页面能力，因此该 API 是生命周期和兼容性边界，不是安全沙箱。

### 4.4 宿主扩展运行时与卸载

建议前端实现统一的 `PluginUiRuntime`，负责拉取清单、按当前页面筛选 contributions、装载资源、排序插槽项、展示加载错误和执行清理。生命周期分为：

1. 主 UI 启动后获取清单，注册所有 `page` 路由，并准备对应 `slot` outlet。
2. 页面进入时创建该页所需 Vue 组件、HTML 片段和 JS module；同时记录组件实例、样式节点、订阅和清理回调。
3. 页面离开时释放页面级资源；插件卸载/清单刷新时再移除路由、全局 contribution、静态资源入口和该插件的缓存引用。

后端 fiber 自动清理只覆盖服务端注册数据，并不能通知已打开的浏览器立即卸载。首版不做 SSE/WebSocket 或浏览器端热更新：插件安装、卸载或配置变化后由用户刷新页面，页面启动时重新拉取完整清单。

### 4.5 页面接入与样式统一

- Sakai SPA 是唯一 UI 壳层；所有 UI 页面由 Vue Router 渲染，插件页面和插槽都复用宿主布局。
- 旧 Python 页面不是长期兼容目标。迁移时把现有 UI 页面改写为 Sakai/Vue 页面，再删除 Python 端的 HTML 页面注册和页面布局；Python API 和静态文件服务继续保留。
- 命名 outlet 只由 Sakai 页面提供，不再为旧 Python HTML 布局维护另一份 bootstrap。插件的 HTML 片段仍可插入 Sakai SPA 中的 outlet，并不是独立完整 HTML 文档。
- 宿主定义语义化 CSS variables、PrimeVue Aura 主题和共享组件；插件 Vue 默认使用 `<style scoped>`，HTML 片段使用带插件命名空间的根 class，插件 JS 注入的 CSS 也要有命名空间并注册清理。
- 动态 loader 注入的 style 要按插件/贡献 ID 记录并清除。页面级 style 不应因路由切换一直留在 `document.head`。
- Tailwind 仍是构建期能力，不能假设运行时 SFC 中的新 class 会自动产生 CSS。插件首版使用共享组件、tokens 和局部普通 CSS；需要 Tailwind/Sass 时考虑预构建插件产物。

### 4.6 资源及组件依赖约定

第一版建议支持 Vue SFC、普通 HTML 片段、原生 ESM 扩展脚本、相对路径导入插件自身资源，以及通过平台 UI SDK 获取 API client 和共享组件。SFC loader 的 `moduleCache.vue` 指向宿主同一 Vue runtime；`getFile` 和 `pathResolve` 只解析同源且已注册插件目录中的资源。普通 ESM 的相对依赖由浏览器从同一静态目录解析。

暂不承诺插件任意 `import` npm 包、Vite 自动组件导入、任意 Sass/Tailwind、独立 Vue Router 或随意注册全局 Vue 插件。确有需要时，扩展有版本的 UI SDK 或评估预构建 ESM，不要依赖 Sakai 未公开的内部组件和 DOM 结构。

## 5. 方案比较

| 方案 | 优点 | 代价 | 建议 |
|---|---|---|---|
| Sakai 主应用构建 + `vue3-sfc-loader` 加载插件 SFC | 插件作者可直接写 `.vue`；不要求每个插件单独构建；可复用宿主 Vue App | loader 维护状态、浏览器运行时开销和依赖映射需要验证 | 适合作为首版快速接入方案。 |
| Sakai 主应用构建 + 插件独立 Vite 构建为 ESM | 编译期检查更完整、依赖与 CSS 可控、性能和生产可预测性较好 | 插件需构建/发布；宿主与插件间需稳定 SDK、Vue 外部依赖和版本兼容协议 | 若插件数量或生产要求增长，优先评估此方案。 |

推荐先以 SFC loader 和宿主 UI runtime 建立功能闭环，同时把 contribution API 与 Vue loader 解耦；这样后续可将资源加载器替换为预构建 ESM，而不改变插件注册、插槽和生命周期契约。

## 6. 主要风险与验证顺序

### 必须先验证

1. **SPA 回退不影响 API**：直达及刷新 `/`、一个二级 Vue 路由、一个无效 `/api/...` 和一个缺失 `/assets/...`，分别得到预期的 SPA、JSON 404、静态 404。
2. **Vue 单实例**：宿主 Sakai 页面和 loader 组件共享同一 Vue runtime；PrimeVue 组件/主题在加载的页面中正常工作。
3. **SFC 最小依赖闭环**：从插件目录加载含 `<script setup>`、相对 `.vue` 导入和 scoped CSS 的组件；确认错误能被捕获，卸载后其 CSS 被清理。
4. **运行时编译与样式注入**：在目标浏览器验证 loader 编译 SFC、执行组件脚本和注入 scoped CSS 的完整流程，并记录首次加载耗时。
5. **资源解析**：验证 SFC 的相对导入、HTML、样式和 ESM 入口均能正确解析到插件静态目录；无效入口可观测地报错，不影响其他插件挂载。

### 已知限制

- 插件脚本与 SFC 在主页面上下文中执行，能调用浏览器和宿主提供的能力；本方案明确以插件可信为前提，不提供代码隔离。
- loader 示例强调网络读取、缓存和样式注入由宿主提供；不能把“loader 能加载 `.vue`”等同于完整插件管理能力。
- Vue 组件卸载不会自动清理插件自行创建的定时器、全局事件或外部订阅；JS 扩展必须返回清理函数，SFC 仍应使用 Vue 生命周期清理副作用。
- 动态注入的 HTML 不自动获得宿主 Vue 行为；需要交互的内容应优先以 Vue contribution 挂载，或由显式 JS 扩展负责事件绑定和清理。
- `D:\sakai-vue` 是工作区外目录。若只在本机引用，其它开发机/CI 无法复现；集成前应确认复制/子模块/独立仓库依赖策略，并保留 MIT LICENSE。

## 7. 建议实施阶段与验收

### 阶段 A：主 UI PoC（已完成）

- 将 Sakai 模板纳入可复现构建流程，生成静态产物。
- 将现有 Python HTML UI 页面逐一迁移为 Vue 页面，并删除对应的 Python 页面注册、字符串布局与欢迎 HTML；保留业务 API 和静态文件服务。
- 后端提供 SPA 入口、静态资源和受限深链回退。
- 验收：主页、迁移后的全部 UI、直接打开/刷新 Vue 深层路由、API、自省 API 和缺失静态资源行为均符合预期；构建不依赖绝对盘符路径，仓库中不再有旧 Python HTML UI 页面入口。

### 阶段 B：插件 UI 注入 PoC（已完成）

- 定义 `register_ui` contribution 契约和 `/api/web/ui`；示例插件同时注册独立页面、一个命名插槽 Vue/HTML contribution 和一个 JS extension。
- 加载器使用宿主 Vue runtime 和 UI SDK；在 Sakai SPA 内验证页面路由、命名 outlet 和 JS extension。
- 验收：三种挂载均可用；菜单/插槽顺序稳定；样式符合平台 tokens；页面离开和插件卸载后路由、组件、style、事件和订阅均清理；一个 contribution 出错不影响其他项。

### 阶段 C：稳定化后决定插件交付规范（待继续）

- 补充错误隔离、缓存策略、插件 UI SDK 兼容版本字段和更多浏览器测试。
- 依据加载性能、PrimeVue 复用和 loader 维护状态，决定继续采用 SFC loader，还是转为 Vite 预构建 ESM。
- 若运行时 SFC 的编译耗时或 loader 维护状态不可接受，则保留注册和生命周期 API，切换为 Vite 预构建 ESM。

## 8. 已确认决策与 PoC 基线

- 第一版同时支持独立页面、Vue/HTML 插槽和 JS extension。
- 所有 UI 页面迁移到 Sakai/Vue，删除旧 Python HTML 页面实现，不为旧页面提供 outlet 兼容层；Python API 和静态文件服务保留。
- 插件安装、卸载或配置变化后由用户手动刷新页面；插件显式配置自己的 `resource_root`。
- 插件视为可信代码；AETP 不配置 CSP，也不提供插件代码隔离。
- PoC 暂以 Windows 当前稳定版 Edge 和 Chrome 为浏览器基线；Firefox/Safari 暂不纳入第一版验收。
- 若部署环境的反向代理或 Web 服务器自行配置了 CSP，需确保其策略允许 `vue3-sfc-loader` 的运行时编译和动态样式；这是外部部署兼容问题，不是 AETP 的安全要求。

## 9. 参考

- Sakai Vue 模板：`D:\sakai-vue`，Vue 3 / Vue Router / PrimeVue / Vite；许可证见模板 `LICENSE.md`（MIT）。
- `vue3-sfc-loader`：https://github.com/FranckFreiburger/vue3-sfc-loader
- 官方用法与限制：https://github.com/FranckFreiburger/vue3-sfc-loader/blob/main/README.md
- 官方示例：https://github.com/FranckFreiburger/vue3-sfc-loader/blob/main/docs/examples.md
- 默认 CommonJS 模块执行实现（使用 `Function`）：https://github.com/FranckFreiburger/vue3-sfc-loader/blob/main/src/tools.ts
- 当前 AETP Web 契约与路由说明：`plugins/web/README.md`、`plugins/web/interface.py`、`plugins/web/router.py`。