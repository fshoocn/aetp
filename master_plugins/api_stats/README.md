# api-stats —— 热点接口统计（拦截链示例插件）

`ctx.webapi.use` 的最小完整示例：`observe` 阶段 / `SKIP` 策略（设计 §4.5、§4.6）。

## 做什么

- **按路由模板聚合**（`GET /api/heartbeat/nodes/{node_id}`）：键数 = 路由数，
  内存天然有界，不会被路径参数打散；
- **排除机器通道**：`/api/heartbeat`、`/api/comm`、`/healthz`（`exclude_prefixes` 可配）；
- **聚合后周期输出**：每 `log_interval`（默认 60 秒）一行
  「热点接口 Top-N（次数 / p95 / 错误率）」，不逐请求写日志；
- **p95 用固定桶直方图**（1 / 5 / 10 / 50 / 100 / 500 / 1000 / 5000 ms），不保存样本；
- **`/healthz` 指标**：`metrics.api_stats` 带累计（`requests` / `errors`）、
  最近窗口（`window_requests`）与 Top-N；
- 错误率按 **5xx** 计（4xx 是客户端问题，计入次数不计错误）；默认只在内存，重启清零。

## 配置（`config.ini` 的 `[api-stats]` 节）

```ini
[api-stats]
log_interval = 60                          ; 热点输出周期（秒）
top_n = 10                                 ; 热点条数
exclude_prefixes = /api/heartbeat,/api/comm,/healthz   ; 排除的路径前缀（逗号分隔）
```

## 用法（核心形态）

```python
async def observe(rc, nxt):
    started, status = perf_counter(), 500
    try:
        response = await nxt()
        status = response.status_code
        return response
    except HTTPException as exc:             # 4xx 也要计入
        status = exc.status_code
        raise
    finally:
        self._record(rc.request.method, rc.route.path, status, perf_counter() - started)

ctx.webapi.use(observe, phase="observe", order=10, on_error=OnError.SKIP)
```

拦截器契约（`RequestContext`）由宿主持有：插件铁律禁止 import 插件包，
本插件用结构化 `Protocol` 鸭子类型对齐（见 `plugin.py` 模块文档）。
