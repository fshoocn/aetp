# CSV 用例生成器（AETP V2 示例插件）

演示「上传的不一定是脚本」：资料驱动 executor 的**自带 UI** 在浏览器内把上传的
CSV/文本逐行生成测试用例，随文件一起提交；Master 收到调用方生成的 `cases` 后
**直接落库**（不调 `parse_cases`）。Agent 执行器为占位（逐 case 直接通过），仅演示
契约。

- 插件 ID：`org.example.csv-cases`
- 版本：`1.0.0`
- 扩展点：`executor`（Master + Agent 双入口 + `entrypoints.ui`）
- 能力：`test.execute`、`test.case-results`

## 目录

```text
csv_cases/
├── plugin.json
├── master/executor.py     # Master 面：create_executor（parse_cases 兜底，通常不被调用）
├── agent/executor.py      # Agent 面：create_executor（占位执行器）
├── ui/index.html          # 自带 UI：上传 CSV → 逐行生成用例 → 提交
└── README.md
```

## 工作流（自定义 UI 生成用例）

1. 在「脚本库」上传 ScriptDefinition，Executor 选择 `org.example.csv-cases`。
2. 弹层内嵌本插件 UI：上传 CSV/文本资料，页面在浏览器内逐行生成用例并预览。
3. 点「提交」→ 插件把 `{ file, configuration, cases }` 经 postMessage 交回宿主。
4. 宿主走认证 multipart 上传，`cases` 随请求携带 → Master 直接落库（`source` + `cases`），
   不调 `parse_cases`。

## 兜底链路（无 cases 时）

若调用方未携带 `cases`（如直接 API 上传），Master 会调本插件 `parse_cases`：读取上传
目录里的 `.csv/.txt/.tsv` 逐行生成相同语义的用例，保证两条链路一致。

## 构建

```powershell
python plugins/build_plugin.py plugins/csv_cases
```

产出 `plugins/org.example.csv-cases-1.0.0.zip`。

## 验证

```powershell
..\..\.venv\Scripts\python.exe -m ruff check plugins/csv_cases
```
