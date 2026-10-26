# 数据库服务（db）

内置插件，随平台直装（不可安装 / 升级 / 启停 / 卸载）。注册 `ctx.db` 服务，
是平台的**统一数据入口**：其他插件声明 `inject = ["db"]` 即可建表与增删改查，
换数据库只改 `config.ini` 的 `[db] url`，不改任何调用方代码。

## 快速上手

```python
from common.plugins.db import Model, Mapped, mapped_column

class TestRun(Model):
    """测试执行记录；逻辑表名取类名蛇形，物理表名由 db 加归属前缀。"""
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]

class MyPlugin:
    inject = ["db"]

    async def init(self):
        runs = self.ctx.db.repo(TestRun)          # 泛型仓储，绑定归属
        await self.ctx.db.sync_schema()           # 建缺表 + 加性对齐 + 补跑迁移
        await runs.add(TestRun(name="nightly"))
```

## 服务分层（一个插件、两个服务）

cordis 一个插件可提供多个服务，且**每个服务的依赖集各不相同**——db 插件
据此拆两层，核心永远不沾 Web 依赖：

| 服务 | 依赖集 | 职责 |
|---|---|---|
| `ctx.db`（DbService） | `appconfig` | 核心数据服务：建表 / 查询 / 归属账本 / 自检引擎；从节点、无头部署照常工作 |
| `ctx.db_ui`（DbUiService） | `db` + `webapi` | 界面层：`POST /api/db/self-check` 与「数据库」页面（`ui/DbPage.vue`） |

`webui` 是**可选依赖**（从节点没有 webui，自检接口仍可用），不写进
`inject`，页面走就绪守护注册（webui 装载 / 换装后自动补，log 查看页同款）；
db 核心因此**永远不需要** `inject = ["webui"]`。依赖缺席时 `db_ui`
安静 PENDING、`ctx.db` 不受影响。

## 能力接口（`ctx.db`）

| 方法 | 说明 |
|---|---|
| `repo(Model, owner=None)` | 泛型仓储：`add / get / list / update / delete / count`（单操作 = 单事务） |
| `session()` | 独立会话（查询表达式全能力），正常退出提交、异常回滚 |
| `transaction(session=None)` | 显式事务；传入 `session` 嵌套即 SAVEPOINT |
| `sync_schema(models=None, migrations=None)` | 建缺表 → 加性对齐（加列 / 加索引 / 放宽长度）→ 执行未跑的迁移步骤，幂等 |
| `execute(stmt)` | 逃生舱：SQLAlchemy Core 原语（`sa.text` 仅接字符串 SQL） |
| `engine` / `dialect` | 逃生舱：异步引擎 / 方言名 |
| `external_model(owner, data)` | 跨插件只读联查的反射映射（需 `dependencies` 声明） |
| `external_table(owner, data)` | 跨插件只读联查的反射表（无主键表的联查逃生舱） |
| `drop_owner(owner, keep_data=False, force=False)` | 按归属清理数据表（卸载链路自动调用） |
| `wait_for_data(owner, data, timeout)` | 等待数据表出现（乱序启动容忍） |

## 表归属与生命周期

- 物理表名 `aetp_{owner}_{logical}`（owner = 插件 id），索引名同样加归属前缀；
- 表由调用方插件认领，**插件卸载时同步清理数据表**（`ctx.plugins.uninstall`），
  不留孤儿表；节点停机 / 插件热更新**不丢数据**；
- 孤儿数据**手动自检**：启动不自动回收（装载顺序会误判内置插件），改由
  `ctx.db.self_check()` / `POST /api/db/self-check` /「数据库」页面按钮触发，
  按 `[db] orphan_policy` 或请求体 `{"policy": "warn" | "drop"}` 决定只报告
  还是清理。

## 跨插件数据依赖（锁步更新）

用其他插件的表必须在 `plugin.json` 声明 `dependencies`：

```json
"dependencies": [
  {"plugin": "test-store", "data": "test_run", "requires": {"run_id": "int", "kind": "str"}}
]
```

- `requires` 是最小数据范围（列 → 类型符），也是唯一校验基准；
- 消费方加载时校验，不符即**拒载并提示更新**；未声明的跨 owner 访问一律拒绝；
- 生产方换结构后平台只**告警**哪些消费方需要更新（不阻断）；被依赖时默认拒绝卸载。

## 配置（`config.ini` 的 `[db]` 节）

```ini
[db]
url = sqlite+aiosqlite:///data/plugins/db/aetp.db   ; 换库唯一入口；env:DB_URL 从环境变量取
echo = false                             ; SQL 日志
pool_size = 5                            ; 连接池大小（非 SQLite）
max_overflow = 10                        ; 池外溢出上限
pool_recycle = 300                       ; 连接回收秒数
orphan_policy = warn                     ; 孤儿表处置：warn / drop
schema_log_limit = 500                   ; 结构变更日志保留上限
```

连接串密码展示时掩码为 `***`，不回显凭证。

路径约定（与 `install_root` / `plugins` 一致）：SQLite **相对库路径**按**节点
目录**（`config.ini` 所在目录）解析——默认库文件落
`master/data/plugins/db/aetp.db` / `slave/data/plugins/db/aetp.db`（专属数据
目录约定，已在 `.gitignore` 忽略），不随当前工作目录漂移。每节点**唯一
数据库**：业务表与治理表同住此库，治理表各归其主——归属登记
（`aetp_db_claims`，含逐表结构指纹与迁移版本）、安装记录
（`aetp_plugin_manager_records`，安装字段分列存放）
与日志查询表（`aetp_log_entries`）同住此库。

## 设计文档

完整设计（选型、表归属、迁移策略、跨插件依赖的锁步模型）见同目录 `DESIGN.md`。
