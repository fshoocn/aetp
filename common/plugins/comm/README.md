# comm —— 主从通信服务（主从通用，全系统唯一主从消息通道）

业务插件**零感知**的数据通道：`ctx.comm.broadcast / send_to / report` 入队即
返回 `message_id`，批量 / QoS 重试 / 背压 / 持久化 outbox / 本地自环由本层
统一承担；传输（默认 HTTP）经 `CommTransport` 契约隔离，换协议不影响调用方。
设计与决策见 [`docs/comm通信方案-2026-10-22.md`](../../../docs/comm通信方案-2026-10-22.md)。

## 接口（`CommInterface`，见 `interface.py`）

```python
# 发送（任意插件）：入队即返回 message_id
mid = ctx.comm.broadcast("task.notice", {...})      # 广播：任一目标成功即可
mid = ctx.comm.send_to("slave-1", "task.assign", {...})   # 定向
mid = ctx.comm.report("log.batch", {"logs": [...]})  # 上行至汇聚点（主节点）

# 投递语义（DeliveryPolicy，可按消息配置）
ctx.comm.report("job", {...}, policy=bounded_policy(min_retries=3, max_attempts=5))
ctx.comm.report("hb", {...}, policy=FIRE_AND_FORGET, immediate=True)  # 心跳类

# 接收：comm.on 与 ctx.on("comm/{topic}") 等价（cordis 事件总线派发）
dispose = ctx.comm.on("log.batch", handler)   # handler(payload, meta)

# 跨节点事件转发
ctx.comm.relay("db/schema-changed", direction="out", mode="broadcast")

# 观测 / 控制
ctx.comm.status(mid)               # {state, attempts, acks, last_error, ...}
await ctx.comm.flush(timeout=5.0)  # 排空在途消息（测试 / 关停）
```

`meta = {"id", "node", "topic", "ts", "received_ts"}`；处理器同步 / 异步均可，
单个处理器异常不影响其他。`DeliveryPolicy` 三参数：`min_success`（至少成功
次数，**任意一次成功即可**）/ `min_retries`（至少重试次数）/ `max_attempts`
（至多发送次数，0 = 不限；**硬上限优先**）。4xx 协议错误直接 FAILED 不重试。

## 行为语义

- **目标**：`[comm] target` 留空 = 本节点即汇聚点（本地自环 + 下行 outbox）；
  填主节点基地址 = 批量上行 `POST /api/comm/ingest`，下行经长轮询
  `POST /api/comm/poll` / `POST /api/comm/ack`
- **广播**：汇聚点扇出逐目标投递，**任一目标 ack 即整条 DONE**（撤销其余）；
  无远端节点时本地派发即计入成功
- **持久化 outbox**：`min_success >= 1` 的消息经 db 插件落 `aetp_comm_outbox`
  （入队即写盘 / 发送成功即删 / 重启续传）；`FIRE_AND_FORGET` 仅内存
- **批量**：攒 `batch_size` 条或 `batch_interval` 秒成批（单批 ≤256 KiB）；
  `immediate=True` 走即时通道不攒批
- **背压**：在途消息 `queue_max` 条封顶，满丢最旧并计数
- **顺序**：单 topic 内按入队顺序派发；跨 topic 不保证
- **自监控**：`/healthz` 的 `comm` 指标（queue / sent / dispatched / dropped /
  retries / rejected / acked / failed / outbox / outbox_disk / relays）

## 配置（`config.ini` 的 `[comm]` 节）

```ini
[comm]
target =                    ; 汇聚点基地址（留空 = 本地自环）
batch_interval = 2.0
batch_size = 200
queue_max = 2000
retry_base = 1.0
retry_cap = 60.0
default_min_success = 1     ; 默认策略（三参数见上）
default_min_retries = 0
default_max_attempts = 0    ; 0 = 不限（at-least-once）
broadcast_ttl = 300.0
poll_hang = 30.0            ; 下行长轮询挂起时长
ack_timeout = 60.0          ; 下行投递后等 ack 时限
outbox_max = 2000
```

**协议不进配置**：HTTP 是默认实现（单选）；将来换协议实现 `CommTransport`
替换默认装配即可。

## 边界

- at-least-once ≠ exactly-once：ack 丢失会重投，处理器需容忍重复（不自动去重）
- 广播"任一成功即可"不承诺全员送达；需要全员送达用逐个 `send_to` 组合
- outbox 不做跨消息事务；持久化依赖 db 插件可用（db 是数据地基，未就绪显式拒绝启动）

测试见 `tests/test_comm.py`。
