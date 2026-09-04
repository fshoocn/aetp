# AETP 插件包目录（V2 Plugin Sources）

本目录是 AETP「万物皆插件」的**插件源工程 + 构建产物**所在地：每个子目录是一个
独立插件工程（含 `plugin.json` 的 Manifest V2），构建脚本产出同目录下
`{plugin_id}-{version}.zip` 归档，供 Master/Agent 插件中心上传安装。

> 归档（`*.zip`）已加入 `.gitignore`，不入库；源码子目录全部入库。需要重新出包时
> 用下方 `--all` 一键重建。

## 插件清单

与《自动化设备测试平台开发规范.md》§142-153 / §397-408 一致：

| 插件源目录 | 插件 ID | point | 装配侧 | 构建产物 | 说明 |
|---|---|---|---|---|---|
| `pytest_plugin/` | `org.pytest.executor` | executor | Master + Agent | `org.pytest.executor-2.0.0.zip` | pytest 用例收集与执行（默认 executor；带 `ui/` 上传/参数界面） |
| `csv_cases/` | `org.example.csv-cases` | executor | Master + Agent | `org.example.csv-cases-1.0.0.zip` | 资料驱动示例：自带 UI 上传 CSV 并生成用例（直存 cases） |
| `aetp_resource/` | `org.aetp.resource` | resource | Agent | `org.aetp.resource-1.0.0.zip` | 串口/电源/Vector CAN 三 Provider（一包多 Provider） |
| `junit_reporter/` | `org.junit.reporter` | reporter | Master | `org.junit.reporter-1.0.0.zip` | JUnit XML → 统一测试结果 |
| `case_statistics_analyzer/` | `org.case-statistics.analyzer` | analyzer | Master | `org.case-statistics.analyzer-1.0.0.zip` | case 统计与质量指标 |
| `console_notifier/` | `org.console.notifier` | notifier | Master | `org.console.notifier-1.0.0.zip` | 通知渠道样例（channel_type=console_test） |
| `quality_gate/` | `org.example.quality-gate` | hook | Master | `org.example.quality-gate-1.0.0.zip` | 准入 Hook 样例（run.before_create） |
| `sharding_case_count/` | `org.case-count.sharding` | sharding | Master | `org.case-count.sharding-1.0.0.zip` | 按 `target_count` 分片 |
| `python_runtime/` | `org.aetp.python-runtime` | runtime | Agent | `org.aetp.python-runtime-1.0.0.zip` | Python 运行时发现 |
| `canoe_software/` | `org.aetp.canoe-software` | software | Agent | `org.aetp.canoe-software-1.0.0.zip` | CANoe 软件/License 发现 |

> 插件 UI 不再依赖独立的 `point=ui`：**任意插件**（主要是 executor）只要在归档里
> 携带 `ui/` 目录并声明 `entrypoints.ui`，就会在脚本上传弹层被同源 iframe 托管，
> 由插件自己决定是否提供上传/配置/用例生成能力（Master 只管静态托管与越界防护）。

## 构建

仓库根目录执行（需 venv 中的 `aetp_protocol` 可导入）：

```powershell
# 构建单个
python plugins/build_plugin.py plugins/junit_reporter

# 一键重建全部（11 个）
python plugins/build_plugin.py --all
```

产物规则见 `plugins/build_plugin.py`：只打包 `plugin.json` + `master/`、`agent/`、
`ui/`、`schemas/` 目录 + 顶层白名单文件（README/LICENSE 等），跳过
`__pycache__`/`.pyc`/`tests`。

## 校验

每个产物都应通过 `PluginArchiveVerifier`（manifest 校验 + 入口文件存在性 + 防越界）：

```powershell
python -c "import sys; sys.path.insert(0,'.'); from pathlib import Path
from aetp_protocol.plugin_archive import PluginArchiveVerifier
for p in sorted(Path('plugins').glob('*.zip')):
    r = PluginArchiveVerifier().verify(p.read_bytes(), filename=p.name)
    print('[OK]', p.name, r.manifest.id.root, r.manifest.point.value)"
```

## 安装与装配

- **Master 侧插件**（executor/reporter/analyzer/notifier/hook/sharding/ui）：经插件中心
  上传 → 验证 → 安装 → 启用 → Master 重启落定（`plugin_governance_service
  .finalize_pending_restarts`），由 `ExtensionResolver` / adapter 桥接加载。
- **Agent 侧插件**（executor/resource/runtime/software）：Master **不会**自动下发，
  需显式调用节点插件同步接口把归档推给 Agent 安装（见下）。
- 各包 README 内含安装/验证说明与自测命令。

## 相关

- 插件契约 / 开发约定见仓库根《插件开发指南.md》。
- 插件化批次与状态表见《自动化设备测试平台开发规范.md》§1.4。
