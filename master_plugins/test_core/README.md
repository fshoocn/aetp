# 测试核心（test-core）

测试任务编排核心：接收任务（Task）→ 拆阶段（Stage）/ 子运行（Job）→ 调度到
从节点执行 → 聚合结果与产物，对外只暴露一个服务门面 `ctx.test_core`。
**是**：Task / Stage / Job / Result 状态机的唯一写者、调度与租约的持有者、
公共契约 [common/test_contracts.py](../../common/test_contracts.py) 的服务实现。
**不是**：测试执行器（跑测试在 `slave_plugins/executor_*`）、上报 / 转存消费方
（Jira 回填、OSS 转存、产物 GC 都是独立消费插件）、cron 调度器与用例仓库管理
（独立插件，经 `submit()` 触发）。协议、状态机与扩展点细节见 [DESIGN.md](DESIGN.md)。

## 功能

- **任务提交**：`submit(TaskSpec)` 幂等提交（`idempotency_key` 去重），线性阶段
  + 屏障执行，支持优先级与 `labels.project` 配额；
- **调度执行**：优先级队列、节点选择、分片（`ShardSpec.mode = "plan"` 由执行器
  规划分片）、租约超时与失联重派、取消 / 重跑失败 Job（attempt+1）；
- **结果聚合**：Job / attempt 明细、用例结果（超 `case_results_limit` 截断）、
  产物引用（`local://` → `oss://` 由消费插件回填）；
- **扩展点**：`test/*` 事件广播（parallel，载荷 = 引用 + 摘要）、三条 HookChain
  数据改写链、任务详情 `extensions` 收集槽；
- **管理界面**：管理 API `/api/test/*`（`TestCoreApiService`）+ webui 页面
  「测试任务」（`ui/TasksPage.vue`，`TestCoreUiService` 注册）。

## 快速开始

主节点 `config.ini` 预装并配置（Web 侧附属服务随依赖自动装载：核心永不被
webapi / webui 卡住）：

```ini
[node]
kind = master
plugins = webui, ../master_plugins/test_core

[test_core]
lease_timeout = 300.0       # Job 租约秒数（发出 → 结果/续期）
job_timeout = 3600.0        # Job 默认超时（StageSpec.timeout_s = 0 时生效）
task_timeout = 0.0          # 任务默认总超时（0 = 不限）
max_attempts = 3            # 单 Job 最大执行次数（含首次）
max_inflight_jobs = 32      # 全局派发窗口
max_inflight_per_node = 4   # 单节点在途上限
default_priority = 0        # 未指定时的优先级
project_quota = 0           # 按 labels.project 的并发配额（0 = 不限）
case_results_limit = 2000   # 单 Job 用例结果截断上限
retention_days = 30         # 产物本地保留天数（GC 插件消费）
history_size = 200          # task_history 单任务保留条数
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `lease_timeout` | `300.0` | Job 租约秒数（发出 → 结果/续期） |
| `job_timeout` | `3600.0` | Job 默认超时（`StageSpec.timeout_s = 0` 时生效） |
| `task_timeout` | `0.0` | 任务默认总超时（0 = 不限） |
| `max_attempts` | `3` | 单 Job 最大执行次数（含首次） |
| `max_inflight_jobs` | `32` | 全局派发窗口 |
| `max_inflight_per_node` | `4` | 单节点在途上限 |
| `default_priority` | `0` | 未指定时的优先级 |
| `project_quota` | `0` | 按 `labels.project` 的并发配额（0 = 不限） |
| `case_results_limit` | `2000` | 单 Job 用例结果截断上限 |
| `retention_days` | `30` | 产物本地保留天数（GC 插件消费） |
| `history_size` | `200` | `task_history` 单任务保留条数 |

## 管理 API 摘要

`kind="api"`，前缀 `/api/test`，鉴权走 webapi 既有 auth 链。成功返回
`{"ok": true, ...}`；失败返回 `{"ok": false, "error": "...", "code": "..."}`
（404 / 400 / 409 / 500，不泄漏堆栈）。

| 方法与路径 | 说明 |
|---|---|
| `POST /api/test/tasks` | 提交任务（body = TaskSpec JSON；`Idempotency-Key` 头可选） |
| `GET /api/test/tasks` | 任务列表（state / labels 过滤、limit / offset 分页） |
| `GET /api/test/tasks/{task_id}` | 任务详情（task + stages + history，含 `extensions` 收集槽） |
| `GET /api/test/tasks/{task_id}/jobs` | Job 列表（含 attempt 历史） |
| `GET /api/test/tasks/{task_id}/result` | 聚合结果 + 各 Job/attempt 明细引用 |
| `POST /api/test/tasks/{task_id}/cancel` | 取消（body 可带 `{"reason": "..."}`） |
| `POST /api/test/tasks/{task_id}/rerun` | 重跑失败 Job（attempt+1） |
| `PATCH /api/test/tasks/{task_id}` | 改优先级（`{"priority": N}`） |
| `GET /api/test/cases` | 用例结果查询（task_id / job_id / status / limit） |
| `GET /api/test/artifacts` | 产物列表（task_id / state） |
| `GET /api/test/artifacts/{artifact_id}/download` | 产物下载（`local://` 回文件；`oss://` 等外部 URI 302） |

管理页「测试任务」（`/tasks`）：任务列表（状态过滤 / 分页）、任务详情
（阶段 / 作业表、状态时间线、产物列表）、取消 / 重跑操作与 `extensions`
扩展信息展示。

## 事件

全部 `parallel` 模式派发（异常隔离，载荷含 `schema_version: 1`；载荷 = 引用 +
摘要，禁止大对象），登记见 [common/event_contract.py](../../common/event_contract.py)：

| 事件 | 时机 | 载荷（摘要） |
|---|---|---|
| `test/task-created` | submit 落库后 | `{task_id, name, labels, source, priority}` |
| `test/task-state` | 每次状态迁移 | `{task_id, from, to, reason}` |
| `test/job-finished` | Job 终结 | `{task_id, job_id, attempt, status, node_id, summary}` |
| `test/case-finished` | 用例失败批（聚合后） | `{task_id, job_id, attempt, failed: [...], truncated}` |
| `test/task-finished` | Task 终结 | `{task_id, state, counts, duration_s}` |
| `test/artifact-ready` | 产物登记后 | `{artifact_id, task_id, job_id, kind, uri, size, sha256}` |

另有任务详情接口的扩展收集槽 `test/collect-task`（监听器把自家数据写进
`response["extensions"]["<插件名>"]`，错误进 `extension_errors`）。

## Hook 扩展点

宿主 = test_core（`ctx.test_core.register_hook`），**修改数据只能走 HookChain**，
拦截器之间禁止直接互调：

| 链 | 触发点 | HookCtx 可改 | 典型用途 | 建议 on_error |
|---|---|---|---|---|
| `task.transform` | submit 落库后、派发前 | `spec`（Stage 参数 / labels） | 按环境注入变量、加标签 | `FAIL` |
| `result.transform` | `test.job.result` 落库前 | `result`（summary / cases） | 归一化 case_id、补标签 | `FAIL` |
| `artifact.sink` | `test/artifact-ready` 发出后 | 只读 `ref`（旁路复制） | 审计旁路写 | `SKIP` |

## 与执行器 / 消费插件的关系

执行器（`slave_plugins/executor_*`）经 comm topic `test.job*` 接活、执行与上报；
消费插件（`reporter_*` / `artifact_oss` / `artifact_gc` 等）只经「事件 + HookChain
+ `ctx.test_core` 只读服务」三条通道参与、**不写核心的数据**。三方约定与协议
细节（载荷结构、版本握手、产物约定、状态机转移表）见 [DESIGN.md](DESIGN.md)
与公共契约 [common/test_contracts.py](../../common/test_contracts.py)。
