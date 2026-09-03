# Case Statistics Analyzer（AETP V2 独立插件包）

对统一测试结果（`UnifiedTestResult`）做 case 统计，产出分析指标：

`total / passed / failed / skipped / duration_ms / failure_rate`

- 插件 ID：`org.case-statistics.analyzer`
- 版本：`1.0.0`（SemVer）
- 扩展点：`analyzer`（`api_version=2.0.0`）
- 装配侧：Master
- 能力：`analysis.case-statistics`

## 目录

```text
case_statistics_analyzer/
├── plugin.json                  # Manifest V2
├── master/
│   ├── __init__.py
│   └── case_statistics_analyzer.py   # CaseStatisticsAnalyzer + create_analyzer()
└── README.md
```

## 用途

Analyzer 在 Reporter 产出统一结果后执行，对 Run 的 case 结果做汇总统计（失败率等）。
分析结果作为独立扩展结果（`run_extension_results`）保存，**不改写原始 case 结果**。

## 使用说明 / 输入位置与格式

- 本插件**不读取外部文件**，输入为平台传入的 `AnalysisRequest.result`
  （`UnifiedTestResult`：`cases` 列表 + `metrics`）。
- case 结果来自 Reporter（如 JUnit Reporter）解析出的统一模型
  （`case_key/status/duration_ms/error_summary`）。
- 统计规则：`failed` 计 `failed/error` 两种状态；`failure_rate = failed / total`
  （`total=0` 时为 `0.0`）。

## 构建 V2 归档

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File plugins\build_plugin.ps1 -PluginDir plugins\case_statistics_analyzer
```

产出 `plugins/org.case-statistics.analyzer-1.0.0.zip`，内容为 `plugin.json` + `master/`。

## 装配（过渡态 → 目标）

- 当前（源码默认装配）：`reporting_pipeline.build_default_reporting_registries` 源码
  import 本包 `CaseStatisticsAnalyzer` 作为兜底。
- 目标（纯插件）：经插件中心上传 → 启用 → `ExtensionResolver.resolve_all(ANALYZER)`
  装配。

## 验证

```powershell
..\..\.venv\Scripts\python.exe -m ruff check plugins/case_statistics_analyzer
..\..\.venv\Scripts\python.exe -m pytest tests/test_reporting_pipeline.py -q
```
