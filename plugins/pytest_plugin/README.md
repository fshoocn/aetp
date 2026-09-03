# AETP pytest V2 executor 插件

V2 executor 插件，用于把 pytest 工程作为测试脚本下发给 Agent 执行：Master 面用
`pytest --collect-only` 解析用例，Agent 面按 Shard Plan 精确执行所选 nodeid，实时采集
stdout/stderr/logging 日志，生成 JUnit XML 并分析出 case 级结果与附件，统一上报给
Master 投影、报告与通知。

```text
pytest_executor.zip（V2 归档）
├── plugin.json          # Manifest V2
├── master/executor.py   # Master 面：create_executor
└── agent/executor.py    # Agent 面：create_executor
```

> 本插件是 V2 executor 的参考实现，也是默认装配的 pytest 执行器。Run 报告解析与
> case 统计由独立的 reporter / analyzer 插件包提供：`plugins/junit_reporter/`
> （`org.junit.reporter`）与 `plugins/case_statistics_analyzer/`
> （`org.case-statistics.analyzer`）。

## 插件标识

- 插件 ID：`org.pytest.executor`
- 版本：`2.0.0`（SemVer）
- 扩展点：`executor`（`api_version=2.0.0`）
- 能力：`test.execute`、`test.case-results`、`test.junit-report`
- 静态准入：需要 Python 运行时（`python`）；动态可用性以 Agent 能力快照为准

## 目录职责

| 文件 | 职责 |
|---|---|
| `master/executor.py` | Master 面 `PytestMasterExecutor.parse_cases`：收集并解析 pytest nodeid 为稳定用例（`stable_key` 即 nodeid） |
| `agent/executor.py` | Agent 面 `PytestExecutor`：`execute` 执行精确 case_keys、`analyze_results` 解析 JUnit、`cleanup`/`cancel` |
| `tests/` | 插件自测（master nodeid 解析、agent JUnit 解析/参数校验/附件收集） |
| `examples/e2e_script/` | 可直接下发的 pytest 冒烟工程（含通过/参数化/跳过/xfail/失败控制用例） |

## 配置项

配置作为 ScriptDefinition / TestTask 绑定配置下发（随 Plan 传到 Agent），字段：

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `python_executable` | str | Agent 当前 Python | Master 收集与 Agent 执行使用的解释器；留空用当前进程 Python |
| `pytest_args` | string[] | `[]` | 附加 pytest 参数；不允许覆盖平台托管的 `--junitxml`/`--rootdir` |
| `fail_fast` | bool | `false` | 首个失败后停止当前 Shard（`--maxfail=1`） |
| `collect_timeout_s` | int | `60` | Master 收集用例超时（秒） |
| `timeout_s` | int | 无（由 Plan 截止时间约束） | 子进程执行超时（秒）；平台另有 Plan deadline 超时 |
| `artifact_paths` | string[] | `[]` | 相对脚本根目录的附件路径/glob，执行后作为 `data` 附件上传 |
| `test_path` | str | 脚本根目录 | 兼容字段（预留）；实际收集范围由上传的脚本工程决定 |

> JUnit XML（`report` 附件）由平台自动上传，不需要写进 `artifact_paths`。

## 构建 V2 归档

在仓库根目录执行：

```powershell
python plugins/build_plugin.py plugins/pytest_plugin
```

产出 `plugins/org.pytest.executor-2.0.0.zip`（`plugin.json` + `master/` + `agent/`）。
也可一键重建 `plugins/` 下全部插件：

```powershell
python plugins/build_plugin.py --all
```

校验归档成员：

```powershell
..\..\.venv\Scripts\python.exe -c "import zipfile; print(zipfile.ZipFile('plugins/org.pytest.executor-2.0.0.zip').namelist())"
```

## 安装与下发流程

1. **上传并安装**：把归档上传到 V2 插件中心
   `POST /api/v2/plugins`，随后 `POST /api/v2/plugins/org.pytest.executor/2.0.0/install`
   （生产写请求带 `Idempotency-Key`）。
2. **启用版本**：`POST /api/v2/plugins/org.pytest.executor/2.0.0/enable`。
3. **上传脚本**：上传 pytest 工程（`.zip` 工程或单文件）。Master 用本插件
   `parse_cases` 收集用例并创建 ScriptDefinition（每个 pytest nodeid 即一个 case）。
4. **创建任务**：新建 TestTask，绑定脚本与用例集合（可多脚本、parallel/sequence）、
   设置上文配置与分片/重试策略，选定项目内已绑定且能力匹配的节点。
5. **触发 Run**：Web/API/定时/CI 触发。Master 生成 ExecutionPlan，经 MQTT
   `aetp/v2/.../execution.plan` 下发给 Agent。
6. **Agent 执行**：按 Plan 的 `case_keys` 精确执行，实时回传日志/进度，结束生成
   JUnit XML 并作为 `report` Artifact 上传。
7. **结果呈现**：Master 投影 Run/case 结果；`JUnitReporter` 与
   `CaseStatisticsAnalyzer` 生成报告与统计；Web Run 详情可查看 case 输出、报告与附件。

## 本地验证

先安装共享协议包（插件本身只依赖标准库，跑通脚本需 Agent 环境有 pytest）：

```powershell
..\..\.venv\Scripts\python.exe -m pip install -e ..\..\common
..\..\.venv\Scripts\python.exe -m ruff check master agent
..\..\.venv\Scripts\python.exe -m pyright master agent
..\..\.venv\Scripts\python.exe -m pytest tests -q
```

仓库端到端测试（真实打包本插件→Agent 安装→执行→JUnit 上报）：

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\test_pytest_executor.py -q
```

可用 `examples/e2e_script/` 作为下发冒烟脚本：覆盖通过/参数化/跳过/xfail，默认不会
失败；需要验证失败终态时在 Agent 运行环境设置 `AETP_E2E_INCLUDE_FAILURE=1`。

## 使用约束

- Agent 节点必须安装 pytest，脚本依赖由 Agent 环境预先安装（运行时发现与预检由平台
  runtime/software 扩展点负责）。
- `--junitxml` 与 `--rootdir` 由平台托管，插件与用户配置不得覆盖。
- 本插件按受信任代码执行，不能视为安全沙箱；只安装经过审核的归档。

