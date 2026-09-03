# Console Notifier（AETP V2 独立插件包）

通知渠道插件：把通知投递打印到 Master 控制台日志（调试/冒烟用）。

- 插件 ID：`org.console.notifier`
- 版本：`1.0.0`（SemVer）
- 扩展点：`notifier`（`api_version=2.0.0`）
- 装配侧：Master
- 能力：`notify.console`
- `channel_type`：`console_test`（与内置 ConsoleSender 相同；启用本插件后覆盖同名内置渠道）

## 用途

Master 启动时把已启用的 notifier 插件注册进 `SenderRegistry`（叠加在内置 sender 之后）。
通知端点 `channel_type=console_test` 时，通知经本插件投递到控制台。

## 目录

```text
console_notifier/
├── plugin.json
├── master/
│   ├── __init__.py
│   └── console_notifier.py   # ConsoleNotifier + create_notifier()
└── README.md
```

## 插件契约（实现要点）

- 工厂 `create_notifier()` 返回带 `channel_type` 与
  `async deliver(NotificationDelivery, secret_value=None) -> DeliveryResult` 的对象。
- 插件**只依赖 aetp_protocol**，不 import Kernel 内部类型；Kernel 用
  `PluginNotificationSender`（master/adapters/notifications/plugin_sender.py）
  桥接成内部 `NotificationSender` 供 dispatcher 调用。

## 构建

```powershell
python plugins/build_plugin.py plugins/console_notifier
```

产出 `plugins/org.console.notifier-1.0.0.zip`。

## 验证

```powershell
..\..\.venv\Scripts\python.exe -m ruff check plugins/console_notifier
```
