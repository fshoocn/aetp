# 心跳上报（heartbeat）

向主模块周期性发送心跳，维护主模块在线状态感知：启动不在线 / 运行中掉线 /
恢复上线三态覆盖，失败自动指数退避重试，**不需要**业务侧再写重试逻辑。

## 功能

- **心跳上报**：按 `interval` 周期向 `POST /api/heartbeat` 上报本节点存活
  （`node_id` / `seq` / `address` 等）；
- **状态机 + 退避**：`unknown` / `online` / `offline` 三态迁移，连续失败按
  `interval × 2^n` 退避（封顶 `max_backoff`），只在状态转换时打日志；
- **配置同步**：`interval` / `timeout` / `max_backoff` 由主模块在心跳应答的
  `config` 字段下发，最迟一个心跳周期生效；`master_url` / `path` 仅本地
  `config.ini` 管理（主模块不可下发）；
- **自省接口**：`GET /api/heartbeat/status` 返回状态快照，可被其他插件扩展。

## 能力接口

### 事件：`heartbeat/collect-status`（`STATUS_EVENT`）

状态接口构造好响应后派发，监听器把自家数据写进
`response["extensions"]["<插件名>"]`（核心字段只读）。并发派发，单个贡献者
出错不影响接口——错误进 `response["extension_errors"]`（恒在），其余贡献
照常返回。

### 事件：`heartbeat/collect-capabilities`（`CAP_EVENT`）

能力信封收集：监听器往传入的列表 `append` 信封条目（推荐 `capability()`
构造），条目随心跳同步到主模块（平时只发摘要 `caps_digest`，变了或主模块
要时才发全量）。信封字段：`kind` / `id` / `provider` 必填（筛选主键），
`version` / `labels` / `summary` 可选，`detail` 任意 JSON（消费方不解析）。

### HTTP：`GET /api/heartbeat/status`

状态快照 + `extensions` 扩展槽 + `capabilities` 能力列表 + `caps_digest`。

## 使用方法

1. 从节点 `config.ini` 启用插件并配置主模块地址：

```ini
[node]
kind = slave
plugins = ../slaveplugins/heartbeat

[heartbeat]
master_url = http://127.0.0.1:8080   # 主模块地址（必填）
interval = 5.0                        # 心跳间隔秒数（启动兜底，主模块可覆盖）
timeout = 3.0                         # 单次请求超时秒数
max_backoff = 60.0                    # 失败重试间隔上限秒数
```

2. 其他插件挂载能力信息（可选）：

```python
from slaveplugins.heartbeat import CAP_EVENT, capability

class MyPlugin:
    def init(self):
        def contribute(bucket):
            bucket.append(capability(
                "executor", "pytest", "pytest-runner",
                summary="Python 单测执行", detail={"cmd": "pytest -q"},
            ))
        self.ctx.on(CAP_EVENT, contribute)
```

3. 运行期自省：`GET http://<从节点>/api/heartbeat/status`。
