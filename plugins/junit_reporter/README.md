# JUnit XML Reporter（AETP V2 独立插件包）

把一次 Run 的 JUnit XML report Artifact 解析为统一测试结果（`UnifiedTestResult`），
供 Analyzer 统计、Web 结果展示与后续通知使用。

- 插件 ID：`org.junit.reporter`
- 版本：`1.0.0`（SemVer）
- 扩展点：`reporter`（`api_version=2.0.0`）
- 装配侧：Master
- 能力：`report.junit-xml`

## 目录

```text
junit_reporter/
├── plugin.json                 # Manifest V2
├── master/
│   ├── __init__.py
│   └── junit_reporter.py       # JUnitReporter + create_reporter()
└── README.md
```

## 用途

Master 在 Run 终态事件（`run.result`/`run.finished`）后执行 Reporter 扩展。本插件从
Run 的 `report` Artifact 中找出 JUnit XML（按 `ArtifactKind.REPORT` + `*.xml`/XML
content-type），解析 `<testsuite>/<testcase>`，把 case 结果（passed/failed/error/skipped、
耗时、`system-out`/`system-err`、失败信息）映射为强类型 `UnifiedTestResult`。Reporter
不修改原始执行事实；只消费 Artifact，产出统一结果。

## 使用说明 / 输入文件位置与格式

- 本插件**不读取外部配置文件**，不依赖固定磁盘位置。
- 输入来自平台 Run 结果中的 report Artifact（由 executor 上传，例如 pytest executor
  生成的 `.aetp-pytest-*.xml`），经 `PluginContext.read_artifact` 读取，内容为
  **标准 JUnit XML**（`testsuite`/`testcase`，含 `name/classname/time` 属性与
  `failure/error/skipped/system-out/system-err` 子元素）。
- 插件只对 `ArtifactKind.REPORT` 且文件名以 `.xml` 结尾（或 XML content-type）的
  Artifact 生效；无匹配 Artifact 时返回空 `ReportResult`（不报错、不改 Run）。

## 构建 V2 归档

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File plugins\build_plugin.ps1 -PluginDir plugins\junit_reporter
```

产出 `plugins/org.junit.reporter-1.0.0.zip`，内容为 `plugin.json` + `master/`。

## 装配（过渡态 → 目标）

- 当前（源码默认装配）：`master/application/services/reporting_pipeline.py` 在
  `build_default_reporting_registries` 中源码 import 本包 `JUnitReporter` 作为兜底，
  resolver 存在时叠加从插件中心解析出的 reporter。
- 目标（纯插件）：去源码兜底后，本插件经插件中心上传 → 启用 → Master
  `ExtensionResolver.resolve_all(REPORTER)` 装配，作为 Run 结果处理的 reporter 扩展。

## 验证

```powershell
..\..\.venv\Scripts\python.exe -m ruff check plugins/junit_reporter
..\..\.venv\Scripts\python.exe -m pytest tests/test_reporting_pipeline.py -q
```
