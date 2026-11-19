# pytest 执行端（executor_pytest，从插件）

pytest 执行器族的**执行端**：接收材料分块与测试任务，完成「材料组装 → 测试执行 →
结果 / 产物 / 日志上报」。**主从分工**：管理端（`master_plugins/executor_pytest`）
负责上传 UI、配方编辑、分析 / 解析编排、选例与任务下发；本插件只管「活怎么干」
——组装（配方解释）、脚本分析、用例解析、执行内核。材料 / 用例 / 选择集的存储归
测试核心（`master_plugins/test_core`，内容不透明），主从同族经 comm 协作。协议
契约见 [DESIGN.md](DESIGN.md)（§5/§6/§7/§10/§11），公共契约见
[common/test_contracts.py](../../common/test_contracts.py)。

## 功能

- **材料接收**：`pytest.material` 自描述分块（每条 chunk 携带 `total/sha256`，
  跨 topic 乱序安全），按 `(material_id, index)` 幂等落盘，齐块校验 sha256，
  坏 / 缺块回 `pytest.material.missing` 请主端补发；材料就绪宽限
  `material_wait_s`（§5.2）；
- **组装**（`assemble.py`）：zip 解包（zip-slip / 路径越界拒绝）→ 按 recipe
  `layout` 铺排（`recipe_override` 浅合并：同名标量 / 对象替换、**数组整体
  替换**）→ 工程目录即 pytest `cwd`；
- **脚本分析 / 用例解析**（`inspect.py`）：真实执行环境、临时目录组装后检查
  包完整性 / 配方 / 语法 / 依赖 / 可收集性（五类问题码，§6.2）；`collect` 经
  `aetp_marker` 插件导出结构化用例清单（`case_id = nodeid`，markers / params
  近似值，§7）；
- **测试执行**（`executor.py`）：argv 化 `python -m pytest`（`shell=False`）、
  超时 / 取消杀**进程树**、junit → `CaseResult`、产物 `junit.xml` + `pytest.log`
  （sha256 / size 自算、`local:///` 盘符 uri）；
- **日志流**（`stream.py`）：`LogChunk` 双写（完整件 + 实时流），50 行 / 2 秒
  批量、节流与单行截断；
- **能力声明**：`executor/pytest`（`detail.schema` 逐字承载 §11.1 参数 JSON
  Schema，管理端据此生成动态表单；`detail.shard = "plan"` 表示支持执行器规划
  分片）。

## 快速开始

从节点 `config.ini` 预装并配置（主端两节见
[管理端 README](../../master_plugins/executor_pytest/README.md)）：

```ini
[node]
kind = slave
plugins = ../slave_plugins/executor_pytest

[executor_pytest]
python = ""                 # 解释器（空 = sys.executable）
pytest_args = "-p no:cacheprovider"
timeout_s = 3600.0          # 单 Job 超时
max_concurrent = 1          # 节点内并发
shard_files = 4             # plan 分片每片文件数上限
workdir = ""                # 默认工作目录（空 = 节点目录）
material_wait_s = 60.0      # 材料宽限等待（§5.2）
material_retain_days = 30.0 # 材料缓存保留天数
material_cache_max_bytes = 2147483648   # 缓存上限（LRU）
log_batch_lines = 50        # 日志流批量行数
log_batch_interval = 2.0    # 日志流批量间隔（秒）
log_rate_limit = 20         # 每秒最多发包数
line_max_bytes = 8192       # 单行截断（字节）
```

## params schema（§11.1 / §11.2）

能力信封 `detail.schema`（JSON Schema 子集，动态发现，D17）：

```python
{"type": "object", "properties": {
    "material_id": {"type": "string"},
    "targets": {"type": "array", "items": {"type": "string"}},
    "extra_args": {"type": "array", "items": {"type": "string"}},
    "keyword": {"type": "string"},
    "env": {"type": "object"},
    "recipe_override": {"type": "object"},
    "selection_id": {"type": "string"},
    "workdir": {"type": "string"}, "python": {"type": "string"}}}
```

| 键 | 说明 |
|---|---|
| `material_id` | 材料引用（组装用；与 `targets/selection_id` 构成执行目标） |
| `selection_id` | 选择集引用（载荷恒小：展开动作在执行端） |
| `targets` | nodeid / 文件 / 目录列表（选例聚合或手填） |
| `extra_args` / `keyword` | 追加参数 / `-k`（并存时 extra_args 优先） |
| `env` | 环境变量增量（**拒 `AETP_*`**，argv 化不进 shell） |
| `recipe_override` | 配方浅合并覆盖（§4.2，数组整体替换） |
| `workdir` / `python` | 覆盖执行端配置 |

未知键忽略；核心注入保留键（`__timeout_s__`）忽略；`material_id` 与
`targets/selection_id` 至少其一。targets 解析优先级：分片 `unit_refs` →
`params.targets` → `params.selection_id` 展开 → `recipe.entry` 兜底。

## 服务与接口摘要

执行端**无 HTTP API**（管理 API 见
[管理端 README](../../master_plugins/executor_pytest/README.md)）；对外是能力
信封与心跳扩展收集槽：

| 接口 | 说明 |
|---|---|
| 能力 `executor/pytest`（`heartbeat/collect-capabilities`） | `detail.schema`（参数契约）+ `detail.shard = "plan"` |
| `heartbeat/collect-status` 扩展槽 | 执行器统计并入节点 status（§14） |
| `stats()` | `queued/running/executed/canceled/timeout/deduped/proto_rejected/logs_sent/logs_dropped/results_reported/material_wait_total` |

## 族内消息协议（§13）

| topic | 方向 | 策略 | 用途 |
|---|---|---|---|
| `test.job` / `test.job.cancel` | 核→从 | at-least-once | 派发 / 取消（契约面） |
| `test.job.result` | 从→核 | at-least-once | 结果（契约面） |
| `test.job.progress` | 从→核 | fire-and-forget | 进度 / material-missing 信号 |
| `test.job.log` | 从→核 | fire-and-forget | 日志流 |
| `pytest.material` / `.chunk` / `.missing` | 主↔从 | at-least-once | 材料分发（自描述分块） |
| `pytest.inspect` / `.result` | 主↔从 | at-least-once | 分析 / 解析（定向单节点） |

- `pytest.*` 族内私有 topic **统一带 `proto` 字段**（不认识版本拒收，D8）；
  `test.*` 是契约面；
- 幂等键：inspect `request_id`（首个应答胜出）、材料 `(material_id, index)`、
  Job `(job_id, attempt)`（登记先于执行，D5）。

## 夹具与自测

集成夹具 `tests/fixtures/pytest_project/`（**非平铺包结构**，验证组装铺排）：

```
tests/fixtures/pytest_project/
├── requirements.txt          # 含一个缺失依赖（测 deps.missing warning）
├── conf/
│   ├── conftest.py
│   └── pytest.ini
└── src/cases/
    ├── case_basic.py         # pass / fail / skip
    ├── case_params.py        # parametrize（nodeid 近似 params）
    └── case_slow.py          # sleep（取消 / 超时杀进程树用）
```

配方 `layout` 把 `src/cases/` 铺到 `tests/`、`conf/` 铺到根目录后才可运行
——对应需求「脚本需组装生成测试工程」的核心场景。

自测命令（仓库根执行，venv 内）：

```powershell
python -m pytest tests/test_executor_pytest.py -q      # 执行端行为测试（9 组）
python -m pytest tests/test_test_core_materials.py -q  # 材料 / 用例 / 选择集服务面
```

9 组覆盖：参数翻译（argv 优先级 / `AETP_*` 过滤 / `shell=False`）、组装
（layout 铺排 / override 浅合并 / zip-slip）、材料接收（乱序分块 / 缺块补发 /
selections 物化）、inspect（五类问题码 / collect 清单 / request_id 幂等）、
执行集成（真子进程跑夹具、junit 映射、产物）、日志流、取消 / 超时杀树、
`(job_id, attempt)` 幂等、proto 拒收。

## 与核心和管理端的关系

- **核心（test_core）**：`test.job*` 的对端——派发 / 租约 / 重试 / 结果聚合归
  核心；材料、用例清单、选择集的**存储与查询**也归核心（D10 数据唯一写者），
  本端只在组装与执行时消费材料内容；
- **管理端（executor_pytest-master）**：`pytest.*` 族内 topic 的对端——预推 /
  补推材料、定向 inspect；任务下发经核心 `submit()`，`params` 透传本端解释；
- 材料内容视为**不可信输入**（路径校验 + argv 化 + `setup` 不执行，D15）；
  collect 会 import conftest，v1 按内部可信处理（D16）。

设计全文与决策记录见 [DESIGN.md](DESIGN.md)。
