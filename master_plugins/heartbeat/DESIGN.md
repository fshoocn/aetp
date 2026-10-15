# 主模块心跳插件设计文档（master_plugins/heartbeat）

> 状态：✅ 已实现（`master_plugins/heartbeat/`，随主节点预装；测试见
> `tests/test_heartbeat_server.py`）。从模块侧（`slave_plugins/heartbeat`）已实现，
> 本文是主模块侧对应插件的实现蓝图；与实现的少量差异见文末「实现备注」。

## 1. 目标

1. **接收从模块心跳**：提供 `POST /api/heartbeat` 接口，接收从模块周期性上报；
2. **维护从模块在线状态**：按「最后心跳时间 + 超时阈值」判定在线 / 离线 / 失联，
   记录状态转换历史；
3. **从模块管理页面**：在主模块 webui 中提供从模块列表页（在线状态、最后心跳、
   心跳频率、移除记录）。

## 2. 与从模块的协议契约

从模块 `slave_plugins/heartbeat` 发送的载荷（`slave_plugins/heartbeat/plugin.py`
的 `_build_payload`）：

```json
POST /api/heartbeat
Content-Type: application/json

{
  "node_id": "从节点",
  "node_kind": "slave",
  "address": "http://127.0.0.1:8081",
  "seq": 42,
  "sent_at": 1760000000.0,
  "status": "alive",
  "caps_digest": "9f2c8a1b4d6e0f37",
  "capabilities": [{"kind": "executor", "id": "pytest", "provider": "pytest-runner"}]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `node_id` | str | 节点标识（`[node].name`，缺省取 kind） |
| `node_kind` | str | 节点类型（`master` / `slave`） |
| `address` | str | 节点自己的 web 地址（可能为空串） |
| `seq` | int | 心跳序号，从 1 起逐次递增；**重置为 1 说明从模块重启过**；到达 `SEQ_MAX`（`2**53-1`，JS 安全整数上限）后**封顶**不再递增（不回绕，避免被误判为重启）。序号在构造载荷时递增，发送失败的尝试也消耗序号 —— `seq` 缺口含重试丢弃，不宜当精确丢包计数 |
| `sent_at` | float | 发送时刻（`time.time()`，从模块时钟，仅参考） |
| `status` | str | 固定 `"alive"`，预留扩展（如 `degraded`） |
| `caps_digest` | str | 能力集摘要（16 位 hex，与条目顺序无关）；无能力时空摘要 |
| `capabilities` | array | 能力信封列表（可选）：首拍 / 摘要变化 / 主模块要时才带，平时不带以省流量 |

主模块应答（2xx）：

```json
{ "ok": true, "server_time": 1760000001.0,
  "config": {"interval": 15.0, "timeout": 3.0, "max_backoff": 60.0},
  "need_caps": true }
```

- **幂等**：同一 `node_id` 重复上报是常态（每 interval 一次），按 upsert 处理；
- **时间基准以主模块接收时刻为准**，`sent_at` 只用于粗略的时钟偏差观测；
- **应答携带 `config` 即配置同步**（已实现，从模块侧见
  `slave_plugins/heartbeat/config.py` 的 `apply_server`）：`interval` / `timeout` /
  `max_backoff` 由**主模块设定**，从模块本地 ini 值只是启动兜底；主模块改配置
  后最迟一个心跳周期自动下发到全部从模块，不需要逐台改 ini。注册后的首次
  心跳应答即返回心跳间隔；`master_url` / `path` 仅由从模块本地 config.ini
  管理（主模块不可下发，防止把从模块指向别的地址），非法值（含 `inf` /
  `nan` / 非有限正数）从模块就地跳过并告警，未知键静默忽略（向前兼容）；
- **应答携带 `need_caps: true` 即请求全量能力**：主模块发现 `caps_digest`
  与存档不一致（或自己刚重启无存档）时回它，从模块下一拍带全量
  `capabilities`；摘要一致时只发 `caps_digest`（能力变化频率低，不必每拍
  重发）。从模块发送失败后下一拍也自动重发全量（宁多勿缺）；
- **应答体保持小巧**：从模块读取上限 256 KiB（`sender.py` 的
  `MAX_RESPONSE_BYTES`），超限按发送失败处理 —— 应答只承载 `config`，
  不要往里塞大对象；从模块不跟随重定向、不走环境代理，3xx 一律视为
  错误状态；
- 从模块把 2xx 之外的状态（4xx/5xx）与网络异常统一视为失败，进入退避重试
  （见 `slave_plugins/heartbeat/sender.py`），因此主模块**不需要**为临时错误返回
  重试指令，只需正常时返回 2xx。

### 2.1 能力信封（capabilities）

「各插件能力格式不确定」的解法：**薄信封 + 不透明载荷**。会被查询 /
筛选的字段必须同构（信封），仅供展示的数据放 `detail` 保持自由——
主模块解析成本与 `detail` 复杂度无关：

```json
{
  "kind": "executor",            // 必填：能力大类（筛选主键）
  "id": "pytest",                // 必填：能力标识（节点内唯一）
  "provider": "pytest-runner",   // 必填：提供者插件名
  "version": "8.3.4",            // 可选
  "labels": {"lang": "python"},  // 可选：自由标签（筛选轴）
  "summary": "Python 单测执行",   // 可选：一句话描述
  "detail": {...}                // 可选：任意 JSON，不解析
}
```

| 字段 | 解析 | 用途 |
|---|---|---|
| `kind` / `id` / `provider` | 严格（缺失 / 空串即坏条目） | 筛选、分组、唯一键 `(node_id, kind, id)` |
| `version` / `labels` / `summary` | 读取，缺失 / 类型不对给默认值 | 筛选、列表列、全文搜索 |
| `detail` | **原样透传** | 详情渲染（通用 JSON 树） |
| 未知信封字段 | 忽略（向前兼容） | — |

收集端（从模块 `slave_plugins/heartbeat/capabilities.py`，主模块直接复用
`parse_capability` / `capabilities_digest` / `select_capabilities`）已保证：
坏条目就地丢弃并上报（进状态接口的 `extension_errors`），条目经 JSON
往返归一化、摘要与顺序无关。

主模块存档 `Capability` 对象（信封字段 + 原始 `detail`），坏条目移入
`unparsed` 桶（管理页单独「未识别」页签原样展示），**不静默丢弃**；
同一 `(node_id, kind, id)` 重复出现时后到覆盖并记日志。

## 3. 状态模型

节点记录（内存态，落盘可后续扩展为 `registry.json`）：

```python
@dataclass
class NodeRecord:
    node_id: str
    node_kind: str
    address: str
    seq: int                 # 最近一次心跳序号
    caps_digest: str         # 最近一次能力摘要
    caps: list[Capability]   # 能力信封（detail 原样）
    unparsed: list[dict]     # 不合信封的条目（不静默丢）
    first_seen: float        # 首次上线时刻（主模块时钟）
    last_seen: float         # 最近心跳时刻（主模块时钟）
    missed: int              # 连续未按时心跳的周期数
    status: NodeStatus       # online / offline / lost
```

### 3.1 在线判定（定时扫描）

一个 **sweeper 后台任务**（每 `sweep_interval` 秒，缺省与 `offline_timeout` 同量级）
扫描全部 `NodeRecord`：

| 条件 | 判定 | 说明 |
|---|---|---|
| `now - last_seen <= offline_timeout` | `online` | 正常 |
| `offline_timeout < now - last_seen <= lost_timeout` | `offline` | 疑似掉线（网络抖动 / 暂停） |
| `now - last_seen > lost_timeout` | `lost` | 长时间失联 |

推荐阈值：`offline_timeout = 3 × interval`（心跳丢 3 次即离线），
`lost_timeout = 10 × interval`。阈值从 `config.ini` 的 `[heartbeat]` 节读取。

状态转换记录到内存环形日志（最近 N 条：时间、节点、旧状态 → 新状态），
供管理页面展示「什么时候掉的线」。

### 3.2 事件

状态转换时广播 cordis 事件，供其他插件（如告警、测试调度）订阅：

```python
ctx.events.emit("heartbeat/node-status", {
    "node_id": "...", "from": "online", "to": "offline", "at": 1760000000.0,
})
```

## 4. API 设计

全部挂在主模块 `ctx.webapi`（`kind="api"`），前缀 `/api/heartbeat`：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/heartbeat` | 接收心跳（协议见 §2），返回 `{ok, server_time}` |
| GET | `/api/heartbeat/nodes` | 从模块列表（含状态、最后心跳、missed、seq） |
| GET | `/api/heartbeat/nodes/{node_id}` | 单个从模块详情 + 最近状态转换历史 |
| DELETE | `/api/heartbeat/nodes/{node_id}` | 手动移除从模块记录 |
| GET | `/api/heartbeat/capabilities` | 跨节点能力查询：`kind`（多选）、`labels.k=v`（子集）、`q`（id/summary 模糊），即 `select_capabilities` 的 HTTP 包装 |
| GET | `/api/heartbeat/config` | 当前生效的阈值配置（只读） |

错误语义对齐 `PluginManagerError`：业务错误返回
`{"error": {"message": ..., "code": ...}}` + 恰当的 HTTP 状态码。

**可扩展返回值**：所有 GET 查询接口与从模块侧
`GET /api/heartbeat/status`（已实现，见 `slave_plugins/heartbeat/plugin.py`
的 `STATUS_EVENT`）采用同一契约 —— 响应构造好后派发
`heartbeat/collect-*` 事件并把响应 dict 传给监听器，其他插件把自家数据
写进 `response["extensions"]["<插件名>"]` 槽位（核心字段只读，避免撞名）；
监听器并发派发，某个贡献者出错不影响接口 —— 错误进 `extension_errors`
列表，其余贡献照常返回。例如设备管理插件可在节点列表里附上自己的
测试任务状态。

## 5. 管理页面（webui）

用 `ctx.webui.register_ui` 登记独立 Vue 页面：

```python
ctx.webui.register_ui(
    "nodes",
    kind="page",
    format="vue",
    resource_root=str(Path(__file__).parent / "ui"),
    entry="NodesPage.vue",
    path="/nodes",
    title="从模块管理",
    menu_group="节点",
    menu_icon="pi pi-desktop",
    menu_order=20,
)
```

页面功能（`master_plugins/heartbeat/ui/NodesPage.vue`，PrimeVue 组件）：

1. **节点列表**：DataTable 展示 `node_id / kind / status / address / last_seen /
   missed / seq`，状态列用 Tag 着色（online 绿 / offline 橙 / lost 红）；
2. **自动刷新**：每 5 秒轮询 `GET /api/heartbeat/nodes`（可暂停）；
3. **操作列**：打开从模块页面（`address` 新窗口）、移除记录；
4. **状态历史**：行展开或侧栏展示该节点最近的状态转换记录。

静态资源由 `resource_root` 声明，随插件卸载自动移除（`register_ui` 的
fiber 绑定语义）。

## 6. 配置（master/config.ini）

```ini
[heartbeat]
interval = 5.0            # 下发给从模块的心跳间隔（秒）
timeout = 3.0             # 下发的单次请求超时（秒）
max_backoff = 60.0        # 下发的失败重试上限（秒）
offline_timeout = 15.0    # 心跳超过该秒数判为 offline（缺省 3 × interval 估算值）
lost_timeout = 50.0       # 超过该秒数判为 lost
sweep_interval = 5.0      # 扫描周期秒数
history_size = 200        # 状态转换历史保留条数
```

配置模型 `HeartbeatServerConfig` 基于 `common.config_base.FieldConfig`
（字段表 + 专属校验：未知键拒绝、正数校验、`lost_timeout > offline_timeout`）。

## 7. 异常与边界场景

| 场景 | 处理 |
|---|---|
| 从模块重启（`seq` 归 1） | 保留 `first_seen` 但记录一次 `restart` 事件，`missed` 清零 |
| 重复 / 乱序心跳（旧 seq） | 仍更新 `last_seen`（不惩罚网络重传），但不推进 `seq` |
| 同一 `node_id` 换地址 | 更新 `address`，记录事件 |
| 能力条目坏（缺必填 / 不可序列化） | 丢弃并归入 `unparsed`（原样展示），不影响心跳与状态机 |
| 能力摘要变化 | 存档替换为新列表，更新 `caps_digest` |
| 主模块重启 | v1 内存态即全量丢失，从模块会自然重新注册（下个心跳周期）；持久化留待后续 |
| 心跳风暴（大量节点同时上线） | 接口无状态、逐条 upsert，天然水平扩展；如需限流走 webapi 层 |
| 从模块时钟漂移 | 不依赖 `sent_at` 判定，一切以主模块接收时刻为准 |
| 恶意 / 非法载荷 | `node_id` 校验（长度、字符集）；超大 body 拒绝（413） |

## 8. 插件结构

```
master_plugins/heartbeat/
    plugin.json          # id: "heartbeat-server", kind: ["master"]
    __init__.py
    config.py            # HeartbeatServerConfig（[heartbeat] 节）
    registry.py          # NodeRegistry：NodeRecord 存储 + 状态扫描 + 事件
    plugin.py            # HeartbeatServerPlugin：装配 API 路由 + sweeper + UI
    ui/
        NodesPage.vue    # 从模块管理页面
```

插件类骨架（`inject` 注入的是**服务**：`appconfig` / `webapi` / `webui` 由
同名插件提供，`node_kind` 是 `common/node_runtime.py` 装配时
`ctx.provide("node_kind", kind)` 注册的值服务，**没有对应插件**）：

```python
class HeartbeatServerPlugin:
    name = "heartbeat-server"
    inject = ["appconfig", "webapi", "webui", "node_kind"]

    def __init__(self, ctx, config=None):
        ...
    async def init(self):
        # 1. POST /api/heartbeat + 管理 API 注册
        # 2. 启动 sweeper 后台任务
        # 3. register_ui 登记 NodesPage.vue
        return self._stop   # 清理函数：取消 sweeper
```

## 9. 测试计划（已落地：`tests/test_heartbeat_server.py`）

1. **协议**：合法心跳 upsert；非法 body（缺 `node_id` / 超大 / 格式错误）拒绝；
2. **状态机**：online → offline → lost 阈值推进；恢复后回落；`seq` 重启检测；
3. **API**：nodes 列表 / 详情 / 移除；capabilities 三轴筛选
   （kind / labels / q）；
4. **集成**：与 `slave_plugins/heartbeat` 对打（复用
   `tests/test_heartbeat.py` 的 `_RecordingMaster` 思路反向验证），
   验证「主模块上线 → 从模块感知」「从模块停 → 主模块判离线」全链路；
5. **清理**：插件卸载后 sweeper 停止、路由注销、UI 贡献移除。

## 10. 实现备注（与设计稿的差异）

实现按本设计落地，少量差异如下（均不影响协议契约）：

- **配置节多三个下发参数**：`[heartbeat]` 增加 `interval` / `timeout` /
  `max_backoff` 作为应答 `config` 的取值来源（§2 的「由主模块设定」需要可配），
  判定阈值缺省仍按 `interval = 5` 估算；
- **「手动标记离线」已裁剪**：维护场景直接移除记录即可，且标记会被下一轮
  扫描立即翻回在线，实际无使用场景；
- **`missed` 进入 `lost` 后冻结**：定格失联前连丢的拍数、不再累计（心跳到达
  归零重计），列表可区分「刚失联」与「失联很久」；
- **扩展槽挂在节点列表 / 详情两个查询接口**（`heartbeat/collect-nodes` /
  `heartbeat/collect-node`）；`capabilities` / `config` 为纯只读查询暂不开
  扩展槽（`heartbeat/collect-capabilities` 已被从模块侧能力信封收集事件占用，
  避免撞名）；
- **管理页用语义化 HTML + 宿主 CSS 变量**：插件 SFC 不能 import npm 包 /
  PrimeVue 组件（见开发文档 §24 约定），以原生表格 + scoped CSS 实现同等功能
  （列表着色、5 秒轮询、操作列、状态历史、未识别条目展示）；
- **错误响应形态**：`{"error": {"message", "code"}}`，`code` 取
  `bad_request` / `not_found` / `payload_too_large`。
