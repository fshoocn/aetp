# Quality Gate Hook（AETP V2 独立插件包）

准入 Hook 样例插件：在 Run 创建阶段（`stage=run.before_create`）评估载荷；示例默认
放行，载荷带 `block=true` 时拒绝（演示）。经 Kernel 桥接注册进 HookRegistry，由
`HookRunner` 执行并审计（fail-closed）。

- 插件 ID：`org.example.quality-gate`
- 版本：`1.0.0`（SemVer）
- 扩展点：`hook`（`api_version=2.0.0`）
- 装配侧：Master
- 能力：`admission.quality-gate`
- 绑定：`name=org.example.quality-gate`、`stage=run.before_create`、`order=100`

## 目录

```text
quality_gate/
├── plugin.json
├── master/
│   ├── __init__.py
│   └── quality_gate.py    # QualityGateHook + create_hook()
└── README.md
```

## 插件契约

- 工厂 `create_hook()` 返回带 `name/stage/order` 与
  `async evaluate(HookEvaluation) -> HookAdmission` 的对象。
- 插件**只依赖 aetp_protocol.hooks**；Kernel 用 `PluginAdmissionHook`
  （master/adapters/hooks/plugin_hook.py）桥接成内部 AdmissionHook。
- 决策映射：`HookAdmission.allowed/reason/code/advisory` → 内部
  `HookDecision.allowed/reason/code/advisory`。

## 构建

```powershell
python plugins/build_plugin.py plugins/quality_gate
```

产出 `plugins/org.example.quality-gate-1.0.0.zip`。

## 验证

```powershell
..\..\.venv\Scripts\python.exe -m ruff check plugins/quality_gate
```
