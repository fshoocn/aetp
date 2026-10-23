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
- 启动时孤儿回收：登记在、插件已不存在的归属按 `[db] orphan_policy` 处理
  （`warn` 告警不删 / `drop` 清理）。

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
data_dir =                               ; 归属登记目录（留空自动推导）
schema_log_limit = 500                   ; 结构变更日志保留上限
```

连接串密码展示时掩码为 `***`，不回显凭证。

路径约定（与 `install_root` / `plugins` 一致）：SQLite **相对库路径**与相对
`data_dir` 按**节点目录**（`config.ini` 所在目录）解析——默认库文件落
`master/data/plugins/db/aetp.db` / `slave/data/plugins/db/aetp.db`（专属数据
目录约定，已在 `.gitignore` 忽略），不随当前工作目录漂移。

## 设计文档

完整设计（选型、表归属、迁移策略、跨插件依赖的锁步模型）见同目录 `DESIGN.md`。
