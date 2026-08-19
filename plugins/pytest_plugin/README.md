# AETP pytest 插件

该目录是项目根目录下的 pytest 任务类型插件，采用 ZIP 插件规范：

```text
pytest_plugin.zip
├── plugin.json
└── main.py
```

## 构建 ZIP

在 `plugins/pytest_plugin` 目录执行：

```powershell
Compress-Archive -Path plugin.json,main.py,ui -DestinationPath ..\pytest_plugin.zip -Force
```

或使用 Python：

```powershell
..\.\.venv\Scripts\python.exe -c "import zipfile; z=zipfile.ZipFile('..\\pytest_plugin.zip','w',zipfile.ZIP_DEFLATED); z.write('plugin.json'); z.write('main.py'); z.close()"
```

## 插件能力

- Master：检查 pytest 脚本、`pytest --collect-only -q` 解析用例、按用例数量分片。
- Agent：执行 pytest，实时采集 stdout/stderr 和 logging，生成 JUnit XML，分析 case 级结果。
- 配置：`pytest_args`、`python_executable`、`timeout_s`、`cases_per_shard`、`artifact_paths`。
- `artifact_paths` 使用相对脚本目录的路径或 glob；JUnit XML 自动上传，声明的附件也会上传到 Master。
- Web Run 详情页的 case 行可展开查看 stdout/stderr，结束产物区域可下载报告和附件。
- 配置页面位于插件包 `ui/index.html`，由 Web iframe 宿主加载；页面通过 `postMessage`
	接收节点能力与验证上下文，不依赖平台 Web 源码。
- 任务类型：`pytest`。
- 版本：`1.0.0`。

## 使用限制

- Agent 节点必须安装 pytest。
- pytest 脚本应包含 `test_*.py` 或 `*_test.py` 文件。
- 脚本中的依赖应由 Agent 环境预先安装。
- ZIP 安装后需要重启 Master 才能加载。
