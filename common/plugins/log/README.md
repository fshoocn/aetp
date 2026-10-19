# log —— 日志服务插件（主从同一份）

给 `ctx.logger` 挂**真实日志出口**（LOG-A1：日志不再只活在内存环形缓冲）。
业务代码照常 `ctx.logger.info(...)`，本插件经 cordis exporter 机制收集并
输出，调用方零改动。

## 出口（一期）

| 出口 | 行为 |
|---|---|
| **console** | error / warn 走 stderr，其余走 stdout，着色 |
| **轮转文件** | 超 `max_bytes` 轮转、留 `backup_count` 份（`.1` 最新）；每条一开一关写，不留常驻句柄（Windows 句柄安全，同元数据短连接取舍） |

文件是**兜底逃生通道**（日志系统自身损坏时 `tail`/`grep` 还能用）；二期
`log.sqlite` 落地后它成为**查询真相源**，两者角色分工见
[`docs/日志方案-2026-10-19.md`](../../../docs/日志方案-2026-10-19.md) §5.2。

## 配置（`config.ini` 的 `[log]` 节）

```ini
[log]
level = info                  ; debug / info / warn / error
console = true                ; 控制台输出开关
file = data/logs/aetp.log     ; 轮转文件（相对节点目录；留空关闭）
max_bytes = 5242880           ; 单文件上限（字节）
backup_count = 3              ; 轮转保留份数
```

`level` 取值非法**显式报错拒载**（不静默回退）；相对路径按 config.ini
所在目录解析，不随启动时的工作目录漂移。

## 用法

```python
class MyPlugin:
    name = "my"

    async def init(self):
        ctx.logger.info("就绪")      # → 控制台 + 文件
```

热更新（`fiber.update` 改 `[log]` 配置）即换出口；卸载本插件出口自动注销。

## 路线图（详见日志方案）

- **二期**：上报 exporter（`ctx.comm`）+ `log.sqlite` 查询库 + `LogsPage.vue`
  查看页；任务输出流接口（`TaskOutputCapture` / `TaskOutputStore`）
- **三期**：动态调级别、API 访问日志、jsonl 文件格式、SSE

测试见 `tests/test_log.py`。
