# Case Count Sharding（AETP V2 独立插件包）

自定义分片插件：按每片最大用例数（`SplitPolicy.target_count`）把一次运行的 cases
切成多个 Shard。纯分片函数，不改 Run/Shard 状态。

- 插件 ID：`org.case-count.sharding`
- 版本：`1.0.0`（SemVer）
- 扩展点：`sharding`（`api_version=2.0.0`）
- 装配侧：Master
- 能力：`sharding.by_case_count`

## 用途 / 触发方式

当 TestTask 的脚本绑定选择 `SplitPolicy(type="custom", plugin_id="org.case-count.sharding")`
且指定 `target_count` 时，Master 触发 Run 会经 `ExtensionResolver` 解析本插件并调用
`split()`，把脚本选中用例按每片最多 `target_count` 个切成多个 Shard。

## 目录

```text
sharding_case_count/
├── plugin.json
├── master/
│   ├── __init__.py
│   └── case_count_sharding.py   # CaseCountSharding + create_sharding()
└── README.md
```

## 构建

```powershell
python plugins/build_plugin.py plugins/sharding_case_count
```

产出 `plugins/org.case-count.sharding-1.0.0.zip`。

## 使用说明（无外部文件）

- 本插件**不读取外部文件**，输入为 `ShardingRequest`（cases/policy/configuration）。
- 分片规则：`target_count` 必须 ≥ 1；按稳定顺序把 `case.stable_key` 每 `target_count`
  个一组切片；无用例时返回一个空 Shard（不报错）。
- 插件只返回分片结果，Kernel 负责落库与状态机。

## 验证

```powershell
..\..\.venv\Scripts\python.exe -m ruff check plugins/sharding_case_count
..\..\.venv\Scripts\python.exe -m pytest tests/test_task_service.py -q
```
