# comm —— 主从通信服务（主从通用）

业务插件**零感知**的数据通道：`ctx.comm.send` 入队即返回，批量 / 退避重试 /
背压 / 本地自环由本层统一承担，传输方式（批量 HTTP → 将来 WS/MQTT）不泄漏
给调用方。设计与决策见 [`docs/日志方案-2026-10-19.md`](../../../docs/日志方案-2026-10-19.md) §5.1。

## 接口

```python
# 发送（任意插件）：fire-and-forget
ctx.comm.send("log.batch", {"logs": [...]})

# 接收（汇聚点）：按 topic 注册处理器（绑调用方 fiber，自动注销）
dispose = ctx.comm.on("log.batch", handler)   # handler(payload, meta)

await ctx.comm.flush(timeout=5.0)             # 排空队列（测试 / 关停）
```

`meta = {"node", "topic", "ts", "received_ts"}`；处理器同步 / 异步均可，
单个处理器异常不影响其他（错误进日志）。

## 行为语义

- **目标**：`[comm] target` 留空 = 本节点即汇聚点（**本地自环**，不走网络）；
  填主节点基地址 = 批量 `POST /api/comm/ingest`（gzip + `X-AETP-Token`）
- **批量**：攒 `batch_size` 条或 `batch_interval` 秒成批（单批 ≤256 KiB）
- **重试**：网络失败 / 5xx / 429 按 `random(base, min(base·2^N, cap))` 退避
  **无限重试**（断网重连不丢）；其余 4xx 协议错误丢该批并计数
- **背压**：队列 `queue_max` 条封顶，满丢最旧并计数（宁丢旧日志不拖垮业务）
- **顺序**：单 topic 内按入队顺序派发；跨 topic 不保证
- **自监控**：`/healthz` 的 `comm` 指标（queue / sent / dispatched / dropped /
  retries / rejected）

## 配置（`config.ini` 的 `[comm]` 节）

```ini
[comm]
target =                    ; 主节点基地址（留空 = 本地自环）
batch_interval = 2.0
batch_size = 200
queue_max = 2000
retry_base = 1.0
retry_cap = 60.0
```

## 刻意不做（fire-and-forget 边界）

确认送达、持久 outbox、RPC / 请求响应——日志类数据容忍丢失；将来重要
事件类需要更强语义时再评估 outbox。

测试见 `tests/test_comm.py`。
