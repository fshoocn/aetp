# Python Runtime Discovery（AETP V2 独立插件包）

runtime 环境发现插件：上报当前 Agent 解释器（`sys.executable`）的 Python 运行时
能力。Provider 拥有 `runtime_type=python`，安装后基础语言扫描不再重复上报 python。

- 插件 ID：`org.aetp.python-runtime`
- 版本：`1.0.0`（SemVer）
- 扩展点：`runtime`（`api_version=2.0.0`）
- 装配侧：Agent
- 能力：`runtime.discovery.python`
- Provider：`provider_id=org.aetp.python-runtime`、`runtime_type=python`

## 目录

```text
python_runtime/
├── plugin.json
├── agent/
│   ├── runtime.py    # PythonRuntimeProvider（discover 当前解释器）
│   └── entry.py      # create_providers()
└── README.md
```

## 插件契约

- 工厂 `create_providers()` 返回 `RuntimeProvider` 元组（本包一个）。
- Provider 实现 `aetp_protocol.discovery.RuntimeProvider`：声明
  `provider_id`/`runtime_type`，`discover() -> tuple[RuntimeCapability, ...]`。
- 插件**只依赖 aetp_protocol**；不触碰 Agent Kernel。
- 默认发现 `sys.executable`（运行 Agent 的解释器）及其版本与 `executable_ref`；
  构造时也可注入 `discoverer` 或预置 `runtimes`（测试/定制场景）。

## 构建

```powershell
python plugins/build_plugin.py plugins/python_runtime
```

产出 `plugins/org.aetp.python-runtime-1.0.0.zip`。
