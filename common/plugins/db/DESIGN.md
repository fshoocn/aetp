# 数据库服务插件设计文档（common/plugins/db）

> 状态：✅ 已实现（2026-10-15）—— §10 决策已全部落定（锁步更新模型）；
> 实现与设计的少量细化见 §12 实现备注。

## 1. 目标与非目标

**目标**

1. **统一数据入口**：内置插件（`common/plugins/db/`，随平台直装）注册 `ctx.db`
   服务，其他插件声明 `inject = ["db"]` 即可建表与增删改查；
2. **换库不改代码**：业务代码只面向 ORM 抽象（模型 / 会话 / 查询表达式），
   更换数据库 = 改 `config.ini` 的连接串（+ 换驱动包），不改任何调用方代码；
3. **表归属插件**：表由调用方插件「认领」，**插件卸载时同步清理它创建的数据表**，
   不留孤儿表；节点正常停机 / 插件热更新**不丢数据**。

**非目标（v1 不做）**

- 全库单链接的 alembic 迁移史（插件生命周期不适合，见 §4.4 的取舍）；
- 跨库数据搬迁 / 多数据源联邦；
- 数据库连接代理、读写分离、分库分表。

## 2. ORM 选型

| | SQLAlchemy 2.x | Peewee | Tortoise ORM | SQLModel |
|---|---|---|---|---|
| 成熟度 / 社区 | 事实标准，最大 | 轻量成熟 | 较新 | 较新（SA 之上） |
| 方言覆盖 | SQLite / MySQL / PG / MariaDB / Oracle / SQL Server / DuckDB… | 主流几种 | 主流几种 | 同 SQLAlchemy |
| 异步支持 | `AsyncEngine` / `AsyncSession`（2.0 完整） | 无原生（线程包装） | 原生 async | 同 SQLAlchemy |
| 换库不改代码 | ✅ 全部面向 Core/ORM 抽象 | ✅ | ✅ | ✅ |
| 迁移工具 | alembic（官方） | playhouse | aerich | alembic |
| 学习成本 | 中（概念多） | 低 | 中（Django 风） | 低 |

**推荐：SQLAlchemy 2.x**（声明式模型 + 异步引擎）。理由：方言最全（换库真正
不用改代码）、2.0 统一 Core/ORM、异步链路成熟、alembic 迁移路径现成。
**备选 SQLModel**：若希望模型自带 Pydantic 校验，可在 SQLAlchemy 之上薄封装，
代价是多一层概念。

**「换库不改代码」的落点**：

- 业务代码只 import 本插件的 `Model` / `repo` / `session`（不直接 import
  `sqlalchemy.dialects.*`）；
- 换库 = 改 `[db] url`（如 `sqlite+aiosqlite:///data/aetp.db` →
  `postgresql+asyncpg://user:pass@host/aetp`）+ 安装对应异步驱动；
- 方言差异（JSON、自增、布尔、分页、标识符长度）集中收敛在本插件内
  （见 §5），并以「同一测试套多库跑通」守护可移植性。

## 3. 服务契约（`ctx.db`，`provide = "db"`）

`ctx.db` 的方法在 **provide 载体**上实现，经代理调用时 `self.ctx` 是调用方
影子 ctx —— 归属识别即由此来（同 webapi 的 `_caller_fiber` 模式）。

### 3.1 模型声明（推荐路径）

```python
from common.plugins.db import Model, Mapped, mapped_column

class TestRun(Model):
    """测试执行记录；逻辑表名取类名蛇形，物理表名由 db 加归属前缀。"""
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    started_at: Mapped[datetime]
    payload: Mapped[dict | None]     # JSON 列（方言差异由 db 兜底）
```

- `Model` 是 SQLAlchemy `DeclarativeBase` 的薄子类：提供**逻辑表名**即可，
  物理表名由注册时改写（`{prefix}{owner}_{logical}`，见 §5）；
- 建表 / 对齐显式触发：`await ctx.db.sync_schema()`（建缺表 + 加性变更自动
  对齐 + 补跑未执行的迁移步骤，幂等，只管本插件认领的表），不隐式建表
  （避免 import 副作用）——演进机制见 §4.4。

### 3.2 仓储 CRUD（日常够用）

```python
runs = ctx.db.repo(TestRun)                 # 泛型仓储，绑定归属
await runs.add(TestRun(name="nightly"))     # 插入
await runs.get(1)                           # 主键查
await runs.list(where=(TestRun.name == "x"), limit=50, offset=0)
await runs.update(1, {"name": "renamed"})   # 改
await runs.delete(1)                        # 删
await runs.count(where=...)
```

### 3.3 会话与事务（进阶）

```python
async with ctx.db.session() as session:     # 独立会话（查询表达式全能力）
    ...
async with ctx.db.transaction():            # 显式事务（嵌套 = SAVEPOINT）
    ...
```

### 3.4 逃生舱

- `ctx.db.execute(stmt)` / `ctx.db.engine`：SQLAlchemy Core 原语；
- `ctx.db.external_model(owner, logical)`：跨插件只读联查的反射模型（见 §4.5）；
- `ctx.db.dialect`：方言名（业务侧极少数需要分支时用，能不用则不用）。

## 4. 表归属与生命周期（核心设计）

### 4.1 归属识别

- 表的 owner = **调用方插件名**（经影子 ctx 的 `_caller_fiber` 识别）；
- 内置 / 直装代码无 registry 记录时 owner 取 fiber 名（如 `appconfig`）；
- 逃生舱：`ctx.db.repo(Model, owner="xxx")` 显式指定（少用，供跨插件协作）。

### 4.2 归属登记（持久化）

`db` 维护**归属表登记**（落数据目录 `data/db/owners.json`，原子写）：

```json
{ "test-runner": {"tables": ["aetp_test_runner_test_run"], "created_at": 1760000000.0} }
```

登记先于建表写入（崩溃后孤儿可回收），drop 成功后删除条目。

结构**指纹登记**与**变更日志**（`aetp_db_meta` / `aetp_schema_log` 表）落在
库内 —— 与 DDL 同事务（消费方加载校验与反查的依据，见 §4.5）。

### 4.3 清理时机：必须区分「卸载」与「停机」

cordis 的 `fiber.dispose()` **不区分场景**：插件热更新（`fiber.update()`）与
节点正常停机（`unload_all`）同样走 dispose + 重建 —— 因此**清理绝不能挂
fiber dispose**，否则改一次配置、重启一次节点就丢光数据。三条路线对比：

| 路线 | 触发 | 热更新 | 停机重启 | 卸载 | 评价 |
|---|---|---|---|---|---|
| A. 挂 fiber dispose | 任何卸载 | ❌ 丢数据 | ❌ 丢数据 | ✅ 清理 | 否决 |
| B. 插件管理器显式清 + 孤儿回收（**推荐**） | `ctx.plugins.uninstall(id)` | ✅ 保留 | ✅ 保留 | ✅ 清理 | 语义正确 |
| C. 纯显式 API | 插件自己 `drop_all()` | ✅ | ✅ | 看插件 | 把责任还给插件，不满足「自动清理」 |

**推荐 B**，清理链路两条：

1. **主动清理**：`ctx.plugins.uninstall(id)`（停用不清，`disable` 只是停服务）
   在删除插件包前调用 `ctx.db.drop_owner(id)` —— 按登记逐表 `DROP TABLE IF EXISTS`
   （幂等，失败重试一次），完成后删登记；清理失败**不阻断卸载**，登记保留并
   `logger.error`；
2. **孤儿回收兜底**：`db` 启动时交叉核对「归属登记」与现存插件
   （`registry.json` + `BUILTIN_PLUGINS`），对已不存在的 owner 按
   `orphan_policy` 处理（默认 `warn`：告警不删，可配 `drop`）——覆盖
   「手删插件目录 / 清理中途崩溃」的残留。

### 4.4 结构演进（迁移）：插件各自声明，db 统一执行

「谁负责表」与「谁做检查」分开看：**表归 owner 插件负责**（结构声明、迁移
步骤都随插件走）；**检查 / 对齐 / 版本记录由 db 统一做**——插件不需要每次
加载手写检查代码，也不需要各自引入 alembic：

| 层级 | 变更类型 | 谁出力 | 机制 |
|---|---|---|---|
| L0 | 首次建表 | db | `sync_schema()` 内 `create_all`（幂等） |
| L1 | 加性变更（加列 / 加索引 / 放宽长度） | **自动，零代码** | 加载时 inspect 现库 vs 模型声明，缺列 `ADD COLUMN`（可空或带默认值）、缺索引补建 |
| L2 | 破坏性 / 语义变更（删列 / 改类型 / 改名 / 回填 / 拆表） | **插件写迁移步骤** | 版本化步骤函数，db 按序执行并记版本 |

插件的结构演进入口只有一个（放 `init`，幂等）：

```python
await ctx.db.sync_schema()   # 建缺表 → 加性对齐 → 执行未跑的迁移步骤
```

L2 的迁移步骤随插件声明（版本号从 2 起递增，与库内 `schema_version` 比对，
只跑未执行的）：

```python
MIGRATIONS = [
    (2, upgrade_v2),   # async def upgrade_v2(conn): ... 例：payload 列改名 + 回填
    (3, upgrade_v3),   # 例：拆出 summary 列
]
```

- 版本记录在库内元表 `aetp_db_meta`（owner → schema_version），**与迁移同事务**：
  步骤失败回滚且版本不前移，重试即续跑（幂等）；
- 步骤是普通异步函数（SQLAlchemy Core / `sa.text` SQL 均可）；复杂场景想用
  alembic 的操作原语也可塞进步骤里，但 **alembic 不做主路径** —— 它的单链
  版本表假设「整个库一条迁移史」，而插件是运行时独立安装 / 卸载的，
  **各自独立的版本序列**更贴合插件生命周期；
- 加载 / 迁移失败 = 插件 `last_error` + 加载中止，**不动已有数据**。

### 4.5 跨插件数据依赖：A 更新表结构，B 怎么办

三条原则先立住：

1. **只有 owner 能动结构**：A 的表只有 A 能迁移 / 删除；B 对 A 的表永远只读
   （非 owner 的 DDL 与迁移一律由 db 拒绝）；
2. **依赖必须显式声明**（进登记，db 才能校验与联动）；
3. **读写分工**：写入 / 命令走服务契约；查询按形态选 b / c（联查统计直读表合法）。

B 访问 A 数据的三种模式（**按访问形态选型**，联查统计见文末速查）：

| 模式 | 做法 | A 换结构时 | 适用 |
|---|---|---|---|
| a. **服务契约** | B `inject = ["a 的服务"]`，调服务拿数据 | 不受影响（契约是服务 API） | 业务读写 / 语义点查（**不适合联查统计**） |
| b. **数据集视图**（可选优化） | A 发布只读 / 聚合视图当数据集（**非必需**） | A 改表时同步维护视图形状 | 高频报表、聚合下推 |
| c. **直读表 + 需求声明** | B 在 `plugin.json` 声明依赖与最小数据范围；结构不符即拒载 | **锁步更新**：A 换结构 → B 须更新后才能加载 | 联查统计、性能敏感 |

模式 c 的机制（**锁步更新（lockstep）模型**：不做多版本兼容 —— A 随时换
结构，跟不上的是消费方；生产方零负担，消费方也没有兼容层要养）：

**① db 侧：结构指纹 + 变更日志**（内置 db 统一维护，**纯审计**，零维护成本）：

- 每次结构变化（`sync_schema` / L2 迁移 / 任何 DDL）自动：更新该表的
  **结构指纹**、写**变更日志**（时间、变动内容 diff、归属插件、插件版本）；
- 指纹按「**范围**」计算：`fingerprint(表, 列范围)` = 规范化子结构
  （列名排序 + 类型归一化）的 sha256 前 16 位；变更日志供排障
  （「B 为什么拒载」直接给出 diff）。

**② 消费方声明**（静态 manifest）：

```json
"dependencies": [
  {"plugin": "test-store", "data": "test_run", "requires": {"run_id": "int", "kind": "str"}}
]
```

- `requires`：**最小数据范围**（列 → 类型），必填 —— 它就是唯一校验基准；
- **强制声明**：B 只要用到 A 的表就必须写 `dependencies` —— db 在
  `external_model` / 跨 owner 访问时**运行时强校验**，未声明一律拒绝访问；
- 可依赖多个插件；字段名 `dependencies`，与既有 `requires`（节点能力标签）区分。

**③ 校验链路**：

- **B 加载时**：反射 A 的表，校验 `requires` 范围内结构**与声明一致**
  （列在 + 类型符）→ 过；否则**拒绝 B 加载**，报「A 的结构已变化（diff 见
  变更日志），请更新本插件」——不猜兼容、无旧指纹白名单中间态；
- **A 换结构后**：db 更新指纹 + 写变更日志，按登记**反查**消费方 → **告警**
  「以下消费方需要更新」（锁步模型：A 自由演进，**不阻断**）；
- **A 表尚不存在时**（全新库 / A 未启用）：**拒绝 B 加载**并报明原因
  （「依赖 `test-store/test_run` 不存在」）——安装 / 启用顺序天然保证「先 A
  后 B」（`plugins` 列表顺序即加载顺序），不搞「先加载后补校验」的宽松语义；
  需要自动容忍乱序的 B 可在 init 里用逃生舱
  `await ctx.db.wait_for_data(owner, data, timeout)` 等表出现；
- **运行中依赖失效**（A 被强制 drop / 重装换表）：`db/schema-changed` 触发
  对消费方复检，未过则停用并报错（已加载的 B 不带着坏依赖继续跑）；
- **A 卸载时**：仍有消费方登记 → 默认**拒绝卸载**（`drop` 可强删）；
- **变更通知**：`db/schema-changed` 广播（owner、diff 摘要），依赖方刷新
  缓存 / 预编译查询用。

⚠️ **边界**：范围校验只防「形状破坏」；**语义变化**（同形状不同含义）不在
自动检测范围，由插件间约定（服务契约）负责。

「语义化版本」不再作为主契约（生产方可不维护版本号）；A 自己的迁移步骤
记账（§4.4 的 `schema_version`）是**内部事务**，与消费方无关 —— A 若只做
L1 加性变更，连记账都不需要。

#### 联查 / 统计：a 不适用，用 b / c

服务契约（a）是**业务语义接口**（写入、命令、点查），不该当查询引擎 ——
B 要 `JOIN + GROUP BY` 统计时，把 SQL 拆进 A 的服务里契约会爆炸，拉原始行
到 B 侧聚合又不划算（聚合该下推给数据库）。这类场景直接用 SQL 的强项：

**技术前提**：所有插件表在**同一个库**（同一连接串），跨 owner 的 JOIN 天然
可行 —— 归属前缀只管「谁能动结构」，不构成查询边界；治理靠依赖声明，
不靠查询路由。

**B 侧联查 A 的表**（不 import A 的代码，反射式只读模型）：

```python
ExternalRun = await ctx.db.external_model("test-runner", "test_run")
stmt = (
    select(MyTable.kind, func.count())
    .join(ExternalRun, ExternalRun.run_id == MyTable.id)
    .group_by(MyTable.kind)
)
rows = (await ctx.db.session().execute(stmt)).all()
```

- `ctx.db.external_model(owner, logical)` 按现库结构反射出**只读**映射
  （无写入 / 无 DDL 能力），B 不需要 A 的 Python 类（插件包互不 import）；
- 配合模式 c 的 `requires` 声明（范围校验，不符则拒载提示更新）；A 表结构
  变化频繁时改走 b（A 发布只读视图，B JOIN 视图）；
- 高频统计建议由 A 直接发布**聚合视图**（含 GROUP BY 的视图 / 物化），
  B 只读结果 —— 契约与性能双赢。

**模式选择速查**：

| 访问形态 | 用哪个 | 说明 |
|---|---|---|
| 写入 / 命令 / 业务语义点查 | a 服务契约 | 强契约，A 换结构不影响 |
| 跨表 JOIN / 聚合统计 | c 直读表（`external_model`）或 b 视图 | SQL 强项，聚合下推数据库 |
| 高频报表 / 慢变统计 | b 聚合视图（A 发布） | 契约与性能双赢 |
| A 表结构频繁演进又不得不联 | b 只读视图 | 视图内做列映射，A 改表不破 B |

### 4.6 演进策略与替代方案盘点

#### 锁步更新（lockstep）：本设计的演进策略

**不做多版本兼容** —— A 更新结构后，消费方 B 跟着更新即可（旧版 B 拒载并
提示更新），不维护兼容窗口 / 多指纹白名单 / 数据集视图契约。理由：插件
同批交付、同步更新的成本远低于长期维护兼容层：

- A **自由演进**（不必 expand-contract 三步走），换结构后平台只**告警**
  哪些消费方需要更新；
- B 的 `requires` 就是唯一兼容基准：符合即加载，不符合就更新 B —— 没有中间态；
- 指纹与变更日志是纯审计设施（自动记录、供排障），不产生维护负担。

#### 统计型负载的两条升级路径（v2 可选）

- **事件投影（CQRS）**：B 不读 A 的表 —— A 变更时广播事件（平台事件总线），
  B 维护自己的投影表做统计；A 的表即事实源、投影可随时重建，
  不需要持久事件日志。适合高频、可容忍最终一致的统计；
- **分析侧车（DuckDB）**：OLTP 走主库，分析查询甩到嵌入式列式库
  （DuckDB 单文件零部署，有 SQLAlchemy 驱动），同步任务供数。
  适合大体量、复杂聚合的报表。

#### 同类问题的既有方案对照

| 方案 / 框架 | 思路 | 适配度 |
|---|---|---|
| Django migrations | **每 app 独立迁移链** + 按 app 记版本 + 跨 app 依赖 | ★ L2 版本链形态的成熟蓝本（不能脱离 Django 用） |
| WordPress `dbDelta` | 激活时声明式对齐表结构（只加不删） | ★ L1 自动对齐的先例 |
| Nextcloud app migrations | 每 app 版本化迁移步骤，升级时补跑 | ★ 与 L2 同构，验证设计 |
| alembic | 全库单链版本表 | ✗ 插件独立装卸对不上；只作步骤内逃生舱 |
| Yoyo / dbmate | 轻量 SQL 迁移，多目录独立版本 | △ 可当 L2 执行器；版本与依赖治理仍要自己做 |
| Schema Registry（Confluent） | 集中 schema + 兼容性规则自动校验 | △ 引申阅读（锁步模型下不需要兼容性分级） |
| dbt model contracts / 数据产品 | 数据集 + 列级契约 + 依赖 DAG | △ 引申阅读（数据集契约已裁剪） |
| Consumer-Driven Contracts（Pact） | 消费方声明期望、生产方跑契约拦截 | ★ 模式 c（需求声明）的思想来源 |
| PostgREST / Hasura | 表自动暴露为 REST/GraphQL | ✗ 与「服务契约优先」冲突，过重 |
| 每插件独立库 + FDW/ATTACH 联查 | 物理隔离 + 跨库查询 | ✗ 跨库无事务、换库成本高，违背「换库不改代码」 |

**结论**：不换架构、不引重型框架；演进走**锁步更新**（不维护兼容层），
统计重负载时再开 v2 的投影 / 侧车路径。

## 5. 命名与多数据库兼容

- **物理表名**：`{prefix}{owner}_{logical}`，`prefix = "aetp_"`；全部小写
  `[a-z0-9_]`；超长时保留前缀 + 逻辑名前段 + 8 位 sha256 后缀防撞
  （`MAX_IDENT = 30` 兜底 Oracle 老版本）；
- **方言差异收敛表**（全部在本插件内消化）：

| 能力 | 策略 |
|---|---|
| 主键自增 | 通用 `Integer` + `primary_key=True`（各方言默认自增） |
| JSON 列 | `sa.JSON`（SQLite 落 TEXT、PG/MySQL 落 JSON） |
| 布尔 / 日期时间 | `sa.Boolean` / `sa.DateTime`（统一 UTC，取用侧转本地） |
| 分页 | `limit/offset`（通用），不做方言私有分页 |
| 大小写 | 标识符全小写（MySQL/PG 默认折叠行为差异回避） |

- **可移植性守护**：同一测试套默认跑 SQLite（零配置），CI 加一个
  PostgreSQL job 跑同一套（`DB_URL` 环境变量注入）。

## 6. 配置（`config.ini` 的 `[db]` 节）

```ini
[db]
url = sqlite+aiosqlite:///data/aetp.db   # SQLAlchemy 异步连接串（唯一换库入口）
echo = false                             # SQL 日志
pool_size = 5                            # 连接池大小
max_overflow = 10                        # 池外溢出上限
pool_recycle = 300                       # 连接回收秒数（防 MySQL wait_timeout）
orphan_policy = warn                     # 孤儿表处置：warn / drop
```

路径约定：SQLite **相对库路径**与相对 `data_dir` 按**节点目录**（`config.ini`
所在目录）解析（与 `install_root` / `plugins` 的相对路径约定一致）——默认库
文件落 `master/data/aetp.db` 等节点目录下的 `data/`（`.gitignore` 已忽略）。

配置模型 `DbConfig` 基于 `common.config_base.FieldConfig`（`ini_section = "db"`，
未知键拒绝、数值正数校验），经 appconfig 的注入 / 写回钩子自动接线 —— UI 改
配置即 `fiber.update` 写回 `[db]` 节。⚠️ `to_dict` / 配置查看接口对
连接串中的**密码掩码**（`user:***@`），不回显凭证。

## 7. 异常与边界

| 场景 | 处理 |
|---|---|
| 连接失败（url 非法 / 服务不通） | **启动失败**（对齐 appconfig「快速暴露」取向），错误进插件 `last_error` |
| 建表冲突（表已被他人占用） | 拒绝并报错（不隐式覆盖）；与登记不一致时以登记为准告警 |
| 事务异常 | 上下文管理器自动回滚；嵌套用 SAVEPOINT |
| drop 表失败（锁 / 连接占用） | 重试一次 → 仍失败则登记保留 + `logger.error`，待孤儿回收 |
| 热更新（`fiber.update`） | 只重建服务与引擎连接池，**不动任何表**（数据保留） |
| 迁移步骤失败 | 同事务回滚、版本不前移（重试续跑）；插件加载中止，错误进 `last_error` |
| 消费方依赖不满足（表不存在 / 缺列 / 类型不符） | 拒绝 B 加载（报明原因并提示更新）；A 换结构后反查仅告警（锁步不阻断） |
| 卸载被依赖的 owner | 默认拒绝（提示先处理依赖方），可配 `drop` 强删 |
| 并发 | 异步引擎连接池；仓储单操作 = 单事务，跨操作一致性由调用方 `transaction()` 保证 |
| SQL 注入 | 全部走 ORM 参数化表达式；逃生舱 `execute` 仅接 `sa.text` 且文档警示 |
| 进程内多插件同名表 | 物理名含 owner 前缀，天然隔离；显式 `owner=` 跨插件写表需两插件同库约定 |

## 8. 插件结构与内置接线

```
common/plugins/db/
    DESIGN.md      # 本文档
    README.md      # 描述文件（插件管理页弹窗）
    __init__.py    # 导出 Model/Mapped/mapped_column/repo 契约
    config.py      # DbConfig（[db] 节，FieldConfig）
    registry.py    # 归属 / 依赖登记 + 指纹与变更日志 + 孤儿回收
    models.py      # Model 基类 + 物理表名改写（归属前缀）
    service.py     # DbService：provide="db"，调用方识别 + repo/session/transaction
    plugin.py      # DbPlugin：装配引擎 + 建表入口 + 启动时孤儿回收
```

**内置接线**（对齐 appconfig/webapi 模式）：

- `common/plugins/webapi/plugin_manager.py` 的 `BUILTIN_PLUGINS` 增加
  `"db"` 条目（随平台直装、禁安装 / 升级 / 启停 / 卸载 / 配置修改）；
- `common/node_runtime.py` 装配顺序：`appconfig` → **`db`** → `webapi` →
  `plugin-manager`（db 依赖 appconfig 读 `[db]`）；
- 插件形态：`inject = ["appconfig"]`、`Config = DbConfig`、不提供 UI；
- `plugin.json` 新增 `dependencies` 字段（数据依赖：`plugin` / `data` /
  `requires` / `fingerprints`，与既有 `requires` 能力标签区分），
  `plugin_manager` 安装时校验格式；
- `ctx.plugins.uninstall` 链路接入 `ctx.db.drop_owner`（见 §4.3）。

⚠️ **禁止反向注入**：`db` **不得** `inject = ["plugins"]`（否则与 plugin-manager
互成循环依赖 —— cordis 无环检测，双方会永远停在 PENDING）；db 需要「现存插件」
信息时（孤儿回收 / 依赖校验）直接读 `registry.json` + `BUILTIN_PLUGINS` 事实源。

## 9. 测试计划

1. **配置**：`[db]` 解析与校验（未知键 / 非法连接串 / 掩码不回显）；
2. **模型与命名**：物理表名改写、长度截断防撞、owner 识别（含显式 `owner=`）；
3. **CRUD / 事务**：add/get/list/update/delete/count、`transaction()` 回滚、
   嵌套 SAVEPOINT（SQLite）；
4. **生命周期**（关键）：`fiber.update()` 后数据保留、节点 `unload_all` 后
   重开库数据保留、`uninstall` 后表被 drop、`disable` 后表保留；
5. **清理鲁棒性**：drop 失败登记保留、孤儿回收 `warn` / `drop` 两策略、
   登记与实际表状态的幂等修复；
6. **可移植性**：同一套用例经 `DB_URL` 切换 SQLite / PostgreSQL 跑通；
7. **迁移**：L1 加性对齐（加列 / 加索引零代码）、L2 步骤按版本续跑、
   失败回滚不前移版本；
8. **跨插件依赖**：`requires` 范围校验（不符拒载并提示更新）、未声明依赖的
   跨 owner 访问被拒、表不存在时拒载报明原因、`wait_for_data` 等表逃生舱、
   A 换结构后反查告警（不阻断）、A 被依赖时卸载拒绝 / 强删两策略、
   运行中依赖失效停用、变更日志记录（时间 / 内容 / 归属 / 插件版本）；
9. **跨插件联查**：`external_model` 反射只读（写 / DDL 被拒）、跨 owner
   JOIN + GROUP BY 聚合下推（同一套用例 SQLite / PG 跑通）；
10. **定界回归**：重复加载 / 重装不重复注册模型（per-owner MetaData）、
    并发 `sync_schema` 串行不竞争、跨 owner 外键被拒、`keep_data` 卸载保留数据。

## 10. 待定决策（请拍板）

1. **ORM 选型**：SQLAlchemy 2.x
2. **清理语义**：§4.3 路线 B「卸载清 / 停机保留」
3. **迁移**：插件声明模型 + 版本化迁移步骤，db 统一执行 / 记版本
   （§4.4，alembic 不做主路径）；
4. **API 形态**：异步 only
5. **依赖接受度**：SQLAlchemy（约 3 MB，MIT）进入平台依赖；
   数据库驱动（aiosqlite / asyncpg / aiomysql）按需选装。
6. **跨插件依赖默认策略**（锁步更新）：`plugin.json dependencies` **强制**
   声明（用 A 的表必须写）、`requires` 最小范围即校验基准（不符拒载并提示
   更新）、未声明的跨 owner 访问拒绝、A 换结构后反查仅告警、被依赖时默认
   **拒绝卸载**；
7. ~~演进纪律~~ **已裁剪**：不做多版本兼容与数据集契约（§4.6 锁步更新）——
   A 更新后消费方全部跟着更新即可。

## 11. 实现定界补充（按此实现，有异议再改）

**必须定界的六条**：

1. **单进程假设**：一个库只被一个节点进程读写；主从各用各库，不共享。
   多进程并发写（SQLite 尤其）是数据损坏级风险 —— 从模块回传数据走
   心跳 / API，不直连主库；
2. **外键策略**：**禁止跨 owner 外键**（联查用查询 `join`，不绑结构）；
   同 owner 内外键用 `RESTRICT`（不用 `CASCADE`，防误删数据）；
3. **Model 注册幂等**：插件重装 / 热更新会重执行模块（`_remove_modules`），
   同名模型在 metadata 重复注册会炸 —— **每个 owner 独立 `MetaData`**
   （顺带隔离 `drop_owner` 与 `external_model` 的作用域）；
4. **逃生舱信任边界**：`ctx.db.execute(sa.text(...))` 可绕过归属约束
   （DDL 不设防）——插件是可信代码（平台既有模型），归属约束只在
   db 的 API 层（repo / sync_schema / drop_owner）强制；
5. **DDL 串行化**：进程内 `asyncio.Lock` 包住全部 DDL（并发 `sync_schema`
   竞争）；异步只影响 I/O 等待，不影响 DDL 互斥；
6. **`owners.json` 路径**：新增 `[db] data_dir`（默认取连接串中库文件所在
   目录；非 SQLite 默认 `data/db/`），相对路径均按**节点目录**（config.ini
   所在目录）解析；测试隔离靠注入此路径。

**实现中顺手补**：热更新时旧连接池 drain（等活跃查询结束再销毁）；
`url = env:DB_URL` 凭证引用（密码不落 config.ini）；**安装时依赖预检**
（装 B 即提示 A 未安装，比加载拒载反馈更早）；`drop_owner(..., keep_data=)`
保留数据开关；类型映射补 `Decimal/UUID/Enum/Binary`、MySQL 默认 `utf8mb4`、
**索引名同样加 owner 前缀**；`aetp_schema_log` 保留上限（防无限增长）；
仓储层异常包装为 `DbError`（逃生舱透传 SQLAlchemy 异常）；测试 fixture
注入临时 `data_dir` + 独立库。

**明确非目标（v1 不做，写死防误期待）**：多进程 / 多节点共享库、跨库事务、
数据搬迁换库、资源配额（表数 / 行数限制）、全文检索与时序扩展。

## 12. 实现备注（与设计稿的细化，均不改设计语义）

1. **会话作用域**：``session()`` / ``transaction()`` 作用域内的仓储操作
   自动汇入当前会话（contextvar），落实 §7「跨操作一致性由调用方
   ``transaction()`` 保证」；传入 ``session=`` 的 ``transaction()`` 仍是
   显式 SAVEPOINT 入口；
2. **``external_model``** 返回 ORM 只读映射类（``Ext.run_id`` 风格属性可直接
   参与 join，正合 §4.5 示例）；无主键表用 ``external_table()``（``.c`` 列
   访问）联查。只读性在 db 的 API 层强制（repo 拒绝），``session()`` /
   ``execute()`` 逃生舱按 §11-4 信任边界放行；
3. **L1 加性对齐拆两趟**：迁移前宽松趟（不可加列先跳过）→ L2 迁移 →
   迁移后严格补漏趟（残留不可加列才报错）——否则「改名」类 L2 迁移会被
   L1 的缺列检查提前拦截；
4. **新建表即基线版本**：``sync_schema`` 本次 L0 刚建的表不重跑历史迁移，
   直接记最新版本为基线（否则新装插件会被旧步骤误伤）；
5. **API 形态**：``sync_schema(models=None, migrations=None)`` —— 无参调用
   （同步调用方认领的全部模型）即 §4.4 的形态，``models=`` / ``migrations=``
   是测试与显式指定的扩展；
6. **SQLite 便利**：文件库父目录不存在时自动创建（默认 ``data/aetp.db``
   直接可用）；内存库自动用 ``StaticPool`` 共享单连接。
