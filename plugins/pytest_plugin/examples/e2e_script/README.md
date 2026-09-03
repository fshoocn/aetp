# AETP pytest 全链路测试脚本

该目录用于在真实 AETP V2 链路上冒烟验证 pytest 下发，不是插件契约测试：

```text
上传 pytest 工程（zip/单文件）
→ Master pytest executor parse_cases 收集用例（pytest --collect-only）
→ 创建/更新 ScriptDefinition
→ 绑定到 TestTask（多脚本、case 选择、分片/重试策略）
→ 触发 Run → Master 生成 ExecutionPlan
→ MQTT execution.plan 下发 → Agent 执行 pytest
→ run 结构化日志 / progress / case 状态回传
→ JUnit XML 分析 → execution.finished
→ Master 状态与 case 结果投影、报告与统计
```

## 用例内容

默认包含：

- 5 个通过或参数化通过用例；
- 1 个 skipped 用例；
- 1 个 xfail 用例；
- 1 个默认跳过的可控失败用例；
- 日志输出和短耗时用例。

默认不让任务失败。若需要验证失败终态，在 Agent 环境设置：

```powershell
$env:AETP_E2E_INCLUDE_FAILURE = "1"
```

注意：平台 Agent 配置遵循外置 `.env` 规则，实际运行时可把该变量写入 Agent 的运行环境或直接移除 `skipif` 标记。

## 本地先验执行

在脚本目录执行：

```powershell
pytest --collect-only -q
pytest -q --junitxml=report.xml
```

查看日志输出：

```powershell
pytest -o log_cli=true -o log_cli_level=INFO -q
```

## 上传前打包

脚本包不是插件包，不需要包含 `plugin.json` 或 `main.py`。从当前目录打包：

```powershell
Compress-Archive -Path test_aetp_e2e.py,pytest.ini -DestinationPath ..\aetp_pytest_e2e_script.zip -Force
```

将生成的 `aetp_pytest_e2e_script.zip` 通过 Web 的脚本上传入口提交；创建 ScriptDefinition
时选择 executor `org.pytest.executor@2.0.0`，平台会用该插件的 `parse_cases` 收集用例。

## 端到端验收观察点

1. Master 日志：脚本解析得到 case（pytest nodeid）。
2. Web Run 详情：出现 pytest 任务的 Run 和 Shard。
3. Agent 日志：出现 pytest 命令、测试 stdout 与执行完成日志。
4. Master 日志/Run 详情：出现结构化日志、case 结果、JUnit 报告与最终状态。
5. 成功场景最终状态为 `succeeded`；开启可控失败后应为 `failed`，并能看到失败 case。
6. 重复 MQTT 投递（相同 plan_hash）不应导致重复执行或重复结果。
