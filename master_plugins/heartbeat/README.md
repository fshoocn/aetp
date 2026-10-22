# 主模块心跳（heartbeat-server）

接收从模块心跳（`slave_plugins/heartbeat`）并维护其在线状态：按「最后心跳时间 +
超时阈值」判定 `online` / `offline` / `lost`，记录状态转换历史，应答携带
下发配置（`interval` / `timeout` / `max_backoff` 由主模块设定）与
`need_caps`（请求全量能力）。协议契约见 [DESIGN.md](DESIGN.md)。

## 功能

- **心跳接收**：`POST /api/heartbeat` upsert 从模块上报（幂等）；应答携带
  `config` 同步运行参数（最迟一个心跳周期生效），`need_caps` 请求全量能力
  （摘要与存档不一致时）；
- **状态机**：`online` / `offline` / `lost` 三态定时扫描推进（阈值可配），
  状态转换记入历史并广播 `heartbeat/node-status` 事件；`seq` 归 1 识别从模块
  重启，换地址同样入历史；`missed`（错过周期）**进入失联后冻结**（定格
  失联前连丢的拍数，心跳到达归零）；
- **能力存档**：按信封解析 `capabilities`（复用 `slave_plugins/heartbeat` 的
  `parse_capability` / `capabilities_digest` / `select_capabilities`），
  坏条目入 `unparsed` 原样展示（不静默丢弃）；
- **管理页**：webui 页面「从模块管理」（`ui/NodesPage.vue`）——节点列表 +
  自动刷新 + 打开从模块页面 / 移除 + 状态历史。

## 能力接口

### HTTP（`kind="api"`，前缀 `/api/heartbeat`）

| 方法与路径 | 说明 |
|---|---|
| `POST /api/heartbeat` | 接收心跳（协议见 DESIGN.md §2），返回 `{ok, server_time, config, need_caps}` |
| `GET /api/heartbeat/nodes` | 从模块列表（状态、最后心跳、missed、seq） |
| `GET /api/heartbeat/nodes/{node_id}` | 节点详情 + 状态历史 + 能力（含 `unparsed`） |
| `DELETE /api/heartbeat/nodes/{node_id}` | 手动移除节点记录（含历史） |
| `GET /api/heartbeat/capabilities` | 跨节点能力查询：`kind`（逗号多选）/ `labels.k=v`（子集）/ `q`（id/summary 模糊） |
| `GET /api/heartbeat/config` | 当前生效配置（只读）+ 本主模块身份 |

业务错误返回 `{"error": {"message", "code"}}` + 恰当状态码（400 / 404 / 413）。

### 事件：`heartbeat/node-status`

状态转换时广播：`{"node_id", "from", "to", "at"}`。其他插件（告警、测试
调度）可订阅；节点列表 / 详情接口另有扩展槽事件
`heartbeat/collect-nodes` / `heartbeat/collect-node`（监听器把自家数据写进
`response["extensions"]["<插件名>"]`，错误进 `extension_errors`）。

## 使用方法

主节点 `config.ini` 预装并配置阈值（详见设计稿 §6）：

```ini
[node]
kind = master
plugins = webui, ../master_plugins/heartbeat

[heartbeat]
interval = 5.0          # 下发的心跳间隔（秒）
timeout = 3.0           # 下发的单次请求超时（秒）
max_backoff = 60.0      # 下发的失败重试上限（秒）
offline_timeout = 15.0  # 超过该秒数判为 offline（缺省 3 × interval）
lost_timeout = 50.0     # 超过该秒数判为 lost（缺省 10 × interval）
sweep_interval = 5.0    # 状态扫描周期（秒）
history_size = 200      # 状态转换历史保留条数
```

⚠️ 依赖 `slave_plugins/heartbeat` 的能力信封模块（`slave_plugins.heartbeat.capabilities`，
协议两侧共用），交付时两者需同时可导入（随仓库 / 同环境安装）。

