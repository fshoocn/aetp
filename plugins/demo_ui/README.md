# Example Demo UI（AETP V2 独立插件包）

UI 扩展样例：由 Master 托管 `ui/` 静态目录，Web Shell 以**同源 iframe** 加载。
页面自包含（不依赖外部 CDN），实现 `aetp.plugin-ui.v2` 消息协议的最小握手。

- 插件 ID：`org.example.demo-ui`
- 版本：`1.0.0`（SemVer）
- 扩展点：`ui`（`api_version=2.0.0`）
- 装配侧：Web/Master
- 入口：`entrypoints.ui = "ui/index.html"`（相对插件根，须位于 `ui/` 下）
- `ui_protocol_version = 2`
- 配置 Schema：`schemas/demo-config.schema.json`

## 目录

```text
demo_ui/
├── plugin.json
├── schemas/
│   └── demo-config.schema.json
├── ui/
│   └── index.html          # 自包含插件页面
└── README.md
```

## 托管与消息

- Master 在 `/plugins/{plugin_id}/{version}/ui` 提供默认文档，`/ui/...` 提供
  `ui/` 目录内静态资源（仅 ENABLED 且声明 `entrypoints.ui` 的 UI 插件）。
- 插件页面**不得**直接请求 Master API；只能通过 `postMessage` 与宿主通信。
- 消息协议 `aetp.plugin-ui.v2`：插件发 `ready` / `configuration.changed`，
  宿主发 `initialize` / `context.updated`；宿主校验 `event.origin`、`event.source`、
  `protocol` 与会话。
- 宿主不因插件 ID 增加特殊分支；示例配置由 Web 宿主保存为结构化配置对象。

## 构建

```powershell
python plugins/build_plugin.py plugins/demo_ui
```

产出 `plugins/org.example.demo-ui-1.0.0.zip`。安装并启用后，打开“插件中心 → 插件 UI”
即可在同源 iframe 中看到本页。
