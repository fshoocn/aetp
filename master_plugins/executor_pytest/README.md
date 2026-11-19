# pytest 执行器管理端（executor-pytest-master）

pytest 执行器族的**管理端**：材料上传面板、组装配方编辑、脚本分析 / 用例解析编排、
选例与材料预推。**主从分工**：本插件管「材料怎么进来、怎么分析 / 预推」（任务下发
并入测试计划编辑器——「测试计划与下发」页，W3 交付）；执行端
（`slave_plugins/executor_pytest`）管「活怎么干」（组装 / 分析 / 执行）；
材料、用例、选择集的**存储与引用**归测试核心（`master_plugins/test_core`，
内容不透明，D10 数据唯一写者）。主从同族经 comm 族内 topic 协作（心跳主从先例）。
协议契约见 [DESIGN.md](../../slave_plugins/executor_pytest/DESIGN.md)（§5/§6/§8/§9），
公共契约见 [common/test_contracts.py](../../common/test_contracts.py)。

## 功能

- **材料管理面板**（`ui/MaterialsPanel.vue`，脚本管理页 `/scripts` 的 slot
  `scripts:executor-pytest`，tab「pytest 脚本」）：zip 上传（内容 + 配方联合
  指纹去重，§4.3；自动带 `X-Material-Executor: pytest` 头标记归属）、配方编辑
  （raw JSON，PUT 后状态回 `uploaded`）、分析 / 解析触发与结果展示（问题按
  severity 着色、用例清单过滤勾选）、选择集保存 / 复制、预推、下载、删除
  （有引用 409 明示）；
- **任务下发**（并入测试计划编辑器——「测试计划与下发」页 `/plans`，W3 交付）：
  原 `ui/SubmitPage.vue` 下发页已退役；参数动态表单（按能力信封
  `detail.schema` 生成 params schema，§11.1，raw JSON 兜底、raw 优先）等逻辑
  随 W3 迁入编辑器，旧文件保留供迁移参考；
- **材料分发**（`material.py`）：自描述分块预推（`push_chunk_bytes` /
  `push_concurrency` 控流）、就绪跟踪、`missing` 缺块补发（§5.2）；
- **inspect 编排**（`plugin.py`）：定向单节点（首个应答胜出）、并发合并、
  超时终结与迟到应答收下刷新（§3.1/§6.1）、结果落核心（problems / cases）。

## 服务拆分

核心 `ExecPytestMasterPlugin`（`provide = "executor_pytest_master"`）只依赖
`appconfig` / `comm`（`ctx.test_core` / `ctx.nodes` 为可选依赖）；Web 侧拆
**附属服务**分批装载（依赖就位自动装载、换装随依赖纪元自动重挂，核心永不被
webapi / webui 卡住）：

| 服务 | 依赖集 | 职责 |
| --- | --- | --- |
| `ExecPytestMasterApiService` | `inject = ["executor_pytest_master", "webapi"]` | 族内管理 API `/api/pytest/*` + 健康指标 |
| `ExecPytestUiService` | `inject = ["executor_pytest_master", "webui"]` | 脚本管理页 pytest 材料 slot 面板（`scripts:executor-pytest`） |

UI 挂载（W2-D）：`ui_service` 注册 slot `pytest-materials-panel`
（`target="scripts:executor-pytest"`，title「pytest 脚本」，面板
`ui/MaterialsPanel.vue`）；旧两页 contribution `pytest-materials` /
`pytest-submit` 已**同批退役注销**（旧路由 `/pytest/materials`、
`/pytest/submit` 不再注册，文件保留供参考）。

## 快速开始

主节点 `config.ini` 预装并配置（执行端一节见
[执行端 README](../../slave_plugins/executor_pytest/README.md)；材料 / 任务
存储需同时预装 `../master_plugins/test_core`，能力查询依赖
`../master_plugins/heartbeat`）：

```ini
[node]
kind = master
plugins = webui, ../master_plugins/test_core, ../master_plugins/heartbeat, ../master_plugins/executor_pytest

[executor_pytest_master]
push_chunk_bytes = 65536      # 分块原文大小
push_concurrency = 2          # 并行预推节点数
inspect_timeout_s = 300.0     # 分析 / 解析应答超时
material_retain_days = 90.0   # 无引用材料保留天数（GC）
```

## 管理 API 摘要

`kind="api"`，鉴权走 webapi 既有 auth 链。成功返回 `{"ok": true, ...}`；失败
返回 `{"ok": false, "error": "...", "code": "..."}`（400 / 404 / 409 / 500 /
503，不泄漏堆栈）。材料 CRUD 由核心承担（页面直连，见
[test_core README](../test_core/README.md)），族内端点不重复实现：

| 方法与路径 | 说明 |
|---|---|
| `PUT /api/pytest/materials/{material_id}/recipe` | 更新配方（body = recipe JSON；状态回 `uploaded` 需重分析） |
| `POST /api/pytest/materials/{material_id}/selections` | 保存选择集 `{"name", "case_ids"}` → `{"ok", "selection_id"}` |
| `POST /api/pytest/materials/{material_id}/push` | 预推材料（`{"selection_ids": [...]}` 可选）→ pushed / failed 节点 |
| `POST /api/pytest/materials/{material_id}/inspect` | 触发分析 / 解析 `{"mode": "analyze"\|"collect"}` → `{"ok", "request_id"}` |
| `GET /api/pytest/inspects/{request_id}` | inspect 状态 / 结果（`inspecting → done / failed`） |
| `GET /api/pytest/stats` | 运行统计（§14） |

面板直连的核心端点（数据源 / API 路径与旧页一致）：`POST /api/test/materials`
（二进制 body + `X-Material-Name` / `X-Material-Recipe` / `X-Material-Executor`
头；名 / 配方含非 Latin-1 字符时改走 `name` / `recipe` 查询参数兜底）、
`GET /api/test/materials`（`limit` / `offset` / `state`，`executor` 过滤）、
`GET /api/test/materials/{id}`、`GET /api/test/materials/{id}/cases`、
`GET /api/test/materials/{id}/download`（下载 zip）、`DELETE /api/test/materials/{id}`
（有引用 409 `material_in_use`）。任务下发 `POST /api/test/tasks` 由测试计划
编辑器经 test_core 发起（W3）。

## 使用流程（脚本管理页 → pytest 面板：上传 → 配方 → 分析 → 选例 → 预推）

1. **上传**（脚本管理页 `/scripts` → tab「pytest 脚本」面板）：选 zip、填材料名
   （可带配方 JSON）→ 上传（自动带 `X-Material-Executor: pytest`）；
   `(sha256, recipe_fingerprint)` 命中既有材料会提示 `deduplicated`；
2. **编辑配方**：详情面板 raw JSON 编辑 `layout/entry/args/env/requirements`
   → 保存（状态回 `uploaded`，配方指纹变化即新指纹；`recipe_override` 合并时
   **数组整体替换**）；
3. **分析 / 解析**：选 mode 发起 inspect（定向单节点，面板轮询
   `/api/pytest/inspects/{request_id}` 至终结）——`analyze` 看问题列表
   （severity 着色），`collect` 产出用例清单；
4. **选例**：用例清单按 marker 过滤、勾选 → 填名称「保存选择集」→ 复制
   `selection_id`（Job 只带引用，载荷恒小）；
5. **预推**：详情面板「预推到候选节点」→ 看 pushed / failed 节点（失败可重试
   或走 missing 兜底）；
6. **任务下发**：转「测试计划与下发」页的测试计划编辑器（W3 交付）——步骤
   引用 `material_id` / `selection_id`、params 按能力信封 `detail.schema`
   动态表单（raw JSON 兜底）→ 下发后跳「测试任务」页看进度。

## 族内消息协议（§13）

| topic | 方向 | 策略 | 用途 |
|---|---|---|---|
| `test.job` / `test.job.cancel` | 核→从 | at-least-once | 派发 / 取消（契约面） |
| `test.job.result` | 从→核 | at-least-once | 结果（契约面） |
| `test.job.progress` | 从→核 | fire-and-forget | 进度 / material-missing 信号 |
| `test.job.log` | 从→核 | fire-and-forget | 日志流 |
| `pytest.material` / `.chunk` / `.missing` | 主↔从 | at-least-once | 材料分发（自描述分块，§5） |
| `pytest.inspect` / `.result` | 主↔从 | at-least-once | 分析 / 解析（定向单节点，§6/§7） |

- `pytest.*` 族内私有 topic **统一带 `proto` 字段**（演进规则同契约：忽略未知
  字段、不认识版本拒收）；`test.*` 是契约面；
- 幂等键：inspect `request_id`（首个应答胜出）、材料 `(material_id, index)`、
  Job `(job_id, attempt)`。

## 与核心和执行端的关系

- **核心（test_core）**：材料 / 用例清单 / 选择集的存储、上传 / 查询 / 删除
  API、`submit()` 与任务状态机；本端经 `ctx.test_core` 服务面读写（不直连 db），
  `params.material_id/selection_id` 由核心透明透传；
- **执行端（executor_pytest）**：`pytest.*` 族内 topic 的对端——接收材料分块、
  应答 inspect、按配方组装并执行；参数语义（§11.2）归执行端解释；
- **心跳（heartbeat-server）**：能力信封查询（`GET /api/heartbeat/capabilities`）
  供测试计划编辑器生成 params 动态表单（W3），节点筛选供预推 / inspect 选节点。

设计全文与决策记录见
[DESIGN.md](../../slave_plugins/executor_pytest/DESIGN.md)。
