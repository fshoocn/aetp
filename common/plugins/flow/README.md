# flow —— 流程引擎（链式执行核心）

把「多步骤、可重试、可恢复、跨插件」的业务流程做成**数据**：节点用
「阶段 + 标签」选择器描述需要的能力（不出现插件名），步骤提供者经
`ctx.flow.step(...)` 自注册，发起方 `ctx.flow.start(...)` 一键启动。
定位与边界、执行语义、预检与覆盖的完整设计见
[`docs/链式执行与流程编排设计-2026-10-26.md`](../../../docs/链式执行与流程编排设计-2026-10-26.md)。

> ⚠️ **Phase 2 内存版**：重启即丢，仅用于开发预览；持久化 / 恢复 / HTTP API
> 是 Phase 3（事件日志为主、`runs` / `steps` 为投影），远程执行是 Phase 4。

## 快速上手

```python
from common.flow_contracts import FlowDef, NodeDef, SlotDef, StepContext

TEST_TASK = FlowDef(
    id="test-task", version=1, title="测试任务",
    nodes=(
        NodeDef("split", "split", locked=True),
        NodeDef("report", "report", labels={"format": "html"}, required=False),
        NodeDef("persist", "persist", always=True),
    ),
    slots=(SlotDef("after-stats", after="stats", stages=("audit", "notify", "report")),),
)

class ReportPlugin:
    inject = ["flow"]

    async def init(self):
        self.ctx.flow.define(TEST_TASK)                       # 模板（绑 fiber）
        self.reg = self.ctx.flow.step(                        # 步骤（绑 fiber）
            "report", "html", self._render,
            labels={"format": "html"}, idempotent=True, summary="HTML 报告",
        )

    async def _render(self, sc: StepContext):
        return {"path": f"reports/{sc.run_id}.html"}          # 输出写黑板

# 发起方
run = await ctx.flow.start("test-task", {"cases": ["a"]}, idempotency_key="ci-1")
await run.wait()                                              # 或订阅 flow/run-finished
```

## 服务接口（`ctx.flow`）

| 方法 | 说明 |
|---|---|
| `step(stage, step_id, handler, ...)` | 注册步骤；`(stage, step_id)` 节点内唯一；返回句柄 `alive / drain() / close()` |
| `define(flow)` | 注册流程模板；同 id 冲突抛 `ValueError` |
| `catalog(stage=None, labels=None)` | 步骤目录（能力信封列表，`kind="flow.step"`） |
| `preflight(flow_id, overrides=None)` | 预检：缺提供者 / 歧义 / 锁定 / 插槽 / 参数（`Problem` 列表） |
| `start(flow_id, input=None, *, overrides, idempotency_key, actor)` | 启动；预检失败抛 `FlowRejected`（不产生半个 run）；返回 `RunHandle` |
| `cancel(run_id, reason="", *, actor=None)` | 取消：在途尝试收到信号，`always` 收尾照常执行 |

## 执行语义（要点）

- **必选 / 可选 / 收尾**：`required` 缺提供者或失败 → run `failed`；可选 → 跳过记
  warning，run 照常 `succeeded`（「测试通过了但报告没生成」）；`always` 失败 /
  取消后仍执行（失败也要保存数据）；
- **重试**：`StepRetryable` / `TimeoutError` / **幂等**步骤的普通异常，指数退避；
- **超时与取消**：置 `sc.cancelled` → `cancel_grace` 协作收尾 → 强制取消；
- **热装卸**：注册绑调用方 fiber，卸载自动摘除；提供者持跨尝试资源时，在自己的
  **同一个**清理函数里先 `await reg.drain()` 再释放（§5.12）；
- **事件**：`flow/run-started`、`flow/node-finished`、`flow/run-finished`、
  `flow/steps-changed`（一律 `parallel`，出错隔离不影响 run 终态）。

## 配置（`config.ini` 的 `[flow]` 节）

见 [`config.py`](./config.py)：并发上限、超时 / 收尾 / 排空 / 等待时长、重试退避
上限、输入输出与黑板上限、远程与保留字段（Phase 3 / 4 启用）。

测试见 `tests/test_flow_engine.py`。
