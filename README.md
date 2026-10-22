# AETP

AETP 是一个基于 cordis（`cordis-port`）插件框架构建的主从式节点平台，目标是搭建**分布式测试平台**。
测试执行、用例生成、结果报告等功能全部由插件提供；主节点负责调度与汇总，多台从节点并行执行测试任务。
新的测试类型（执行器、用例来源、报告格式）以插件方式扩展，不需要改动平台内核。

## 特性

- **插件化内核**：基于 cordis 的依赖注入、服务、事件总线与自动清理，插件可独立安装、启停、热更新与卸载。
- **主从节点**：`master/`（带 Web UI 的主节点）与 `slave/`（无头从节点）共用同一套运行时，仅配置不同。
- **统一的数据与配置**：内置数据库服务（SQLAlchemy 异步，默认 SQLite）和 `config.ini` 应用配置，换库只改一行配置。
- **Web 能力**：基于 uvicorn + Starlette 的 HTTP API，主节点附带 Vue 3 + PrimeVue 的管理界面（插件管理、从节点管理等）。
- **主从通信**：心跳上报（状态机 + 指数退避）与批量数据通道（日志等数据的批量上报与重试）。
- **日志**：控制台与轮转文件输出，并可汇聚上报到主节点。

## 当前已具备的能力

以下能力已在仓库中实现，可以直接使用：

- **插件生命周期**：插件可上传 ZIP 包、启用、停用、热更新配置或卸载，无需重启节点。
- **主从节点网络**：主节点（`master/`）带 Web 界面；从节点（`slave/`）无头运行；从节点通过心跳上报状态，主节点判定在线 / 离线 / 失联并保留历史。
- **能力清单**：插件可声明本节点具备的能力，心跳会把它同步到主节点（当前是否用于调度属于规划内容）。
- **HTTP API 与页面**：插件通过 `ctx.webapi` 注册接口（同步或异步处理函数均可），通过 `ctx.webui` 注册页面、插槽或 JS 扩展。
- **统一数据存储**：插件通过 `ctx.db` 建表和增删改查，表归属于插件，卸载时可清理；结构变更支持自动对齐与版本化迁移，换库只需修改 `[db] url`。
- **主从数据通道**：插件通过 `ctx.comm.send` 发送数据，批量攒包、退避重试，队列满时丢弃最旧数据；接收方用 `ctx.comm.on` 按主题订阅。
- **配置与日志**：各节点行为由 `config.ini` 控制，插件通过 `ctx.appconfig` 读取并可写回；日志输出到控制台与轮转文件。

## 规划中的测试平台能力

以下能力是项目的主要建设方向，**目前尚未实现**（仓库中还没有测试执行类插件）：

| 能力 | 说明 |
|---|---|
| pytest 执行 | 在从节点上运行 pytest 用例，回收结果与日志 |
| CANoe 执行 | 调用 CANoe 执行测试模块，回收结果 |
| CDD 用例生成 | 解析 CDD 诊断描述文件，自动生成测试用例 |
| 任务拆分与多机并行 | 把一次测试拆成多个小任务，按节点能力分派到多台从节点同时执行 |
| 失败重试 | 任务失败后按次数与退避策略重试，记录每次尝试 |
| 结果汇总与查看 | 结果经数据通道回传主节点入库，在 Web 页面统一查看 |

> 说明：当前通信层为 fire-and-forget（不保证送达、没有请求 / 应答）。任务派发确认与失败重试需要在它之上增加调度层，这是测试平台的核心开发任务之一。详见 [docs/开发文档.md](docs/开发文档.md)。

## 使用示例

### 示例一：多机并行执行测试（规划中的流程）

1. 每台测试机的从节点在 `config.ini` 的 `plugins` 中列出 `pytest` 与 `canoe` 执行插件，启动后通过能力信封声明自己具备的能力（如 `canoe`、`pytest`）。
2. 主节点导入一套测试用例（例如由 CDD 生成的用例），调度插件把用例拆成多个小任务。
3. 调度插件按能力与空闲情况把任务派发到对应从节点并行执行。
4. 某个任务失败时，按重试策略重新派发到其他节点，并记录每次尝试。
5. 结果经数据通道回传主节点，写入数据库，在 Web 页面查看汇总报告。

### 示例二：用插件扩展一个测试结果报告（当前即可实现）

这个插件组合了 `webapi`、`comm`、`db` 三类服务：订阅从节点通过 `test/result` 主题发来的结果并写入数据库，同时通过 `ctx.webapi` 暴露一个汇总查询接口。

插件目录只需要一个 `plugin.json`（声明 `id`、`entrypoint`、`kind: master` 等元数据）和入口代码。把插件目录放进 `master_plugins/`，并在主节点 `config.ini` 的 `[node] plugins` 中加入 `../master_plugins/test-report`，重启即可安装启用；也可以把目录打成 ZIP，在插件管理页面上传。

### 其他扩展方向

- **CI 集成**：通过 `ctx.webapi` 暴露触发测试、查询结果的接口，让 Jenkins、GitLab CI 等在构建流程中调用平台。
- **设备 / 台架监控**：从节点用能力信封上报台架是否可用、CANoe 是否就绪，主节点调度时据此筛选目标节点。
- **通知**：订阅测试完成相关事件（见开发文档第 9 节事件总线），把结果推送到企业微信、邮件等。
- **报告导出**：生成 JUnit XML、Excel 或 HTML 报告，用于 CI 展示或归档。
- **自定义执行器**：任何能执行命令并返回结果的工具（如 Robot Framework 或自研脚本）都可以包装成一个执行插件接入平台。

开发插件的详细说明见 [docs/开发文档.md](docs/开发文档.md)。

## 目录结构

```text
aetp/
├── common/                 # 主从共用的运行时与内置插件
│   ├── node_runtime.py     #   节点装配与退出流程（master / slave 共用）
│   ├── cordis_utils.py     #   cordis 辅助工具
│   ├── config_base.py      #   配置模型基类
│   └── plugins/            #   内置插件（随平台直装）
│       ├── appconfig/      #     应用配置（config.ini）
│       ├── comm/           #     主从通信（批量发送、退避重试）
│       ├── db/             #     数据库服务
│       ├── log/            #     日志服务
│       └── webapi/         #     Web 服务、路由与插件管理（plugin-manager）
├── master/                 # 主节点
│   ├── main.py             #   入口（薄启动器）
│   ├── config.ini          #   主节点配置
│   ├── webui/              #   Web UI 宿主插件（含 Vue 前端 frontend/）
│   └── plugins/            #   运行时安装目录（由插件管理写入，已 gitignore）
├── slave/                  # 从节点（无头）
│   ├── main.py             #   入口（薄启动器）
│   ├── config.ini          #   从节点配置
│   └── plugins/            #   运行时安装目录（已 gitignore）
├── master_plugins/         # 主节点专属插件源（如 heartbeat：接收心跳、管理从节点）
├── slave_plugins/          # 从节点专属插件源（如 heartbeat：向主节点上报心跳）
├── docs/                   # 开发文档与设计资料
├── requirements.txt        # 运行时依赖
├── requirements-dev.txt    # 开发依赖
└── pyproject.toml          # ruff / pyright 工具配置
```

> 插件源（`common/plugins/*`、`master/webui`、`master_plugins/*`、`slave_plugins/*`）是未安装的源码；
> 节点启动或通过插件管理安装后，才会落到各节点的 `plugins/` 安装目录中。

## 环境要求

- Python **3.14**（以 `pyproject.toml` 中的 `pythonVersion` 为准）
- Node.js 与 npm（仅在构建主节点 Web UI 时需要）

## 快速开始

### 1. 安装 Python 依赖

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. 构建主节点 Web UI（可选）

如果需要图形界面，先构建前端产物（输出到 `master/webui/static/ui/dist/`）：

```powershell
Push-Location master/webui/frontend
npm ci
npm run build
Pop-Location
```

跳过这一步时主节点仍可运行，只是没有网页界面，仅提供 API。

### 3. 启动主节点

```powershell
python master/main.py
```

默认监听 `http://127.0.0.1:8080`。启动后可在浏览器打开该地址访问管理界面。

### 4. 启动从节点

另开一个终端：

```powershell
python slave/main.py
```

从节点默认监听 `http://127.0.0.1:8081`，并通过 `slave/config.ini` 中 `[heartbeat] master_url` 指向的主节点上报心跳。
从节点不安装 Web UI，页面路径返回 JSON 404，查看页面统一在主节点完成。

按 `Ctrl+C` 可有序退出节点。

## 配置

每个节点目录各有一份 `config.ini`，相对路径均按该文件所在目录解析。主要配置节如下：

| 节 | 作用 | 关键项 |
|---|---|---|
| `[node]` | 节点自身信息 | `kind`（`master` / `slave`，必填）、`name`、`install_root`（插件安装目录，默认 `plugins`）、`plugins`（预装插件源，逗号分隔） |
| `[web]` | HTTP 服务 | `host`、`port`、`cors_origins`、`auth_token`（节点通信令牌） |
| `[db]` | 数据库 | `url`（SQLAlchemy 异步连接串，默认 `sqlite+aiosqlite:///data/aetp.db`）、`echo`、连接池参数 |
| `[log]` | 日志 | `level`、`console`、`file`（轮转文件）、`max_bytes`、`backup_count` |
| `[comm]` | 主从数据通道 | `target`（主节点地址，留空则本地自环）、批量与退避参数 |
| `[heartbeat]` | 心跳 | 主节点：`interval` / `timeout` / `offline_timeout` 等；从节点：`master_url`、`interval` 等 |

几点注意：

- **`auth_token` 必须修改**：默认值 `aetp-dev-token` 仅用于开发。生产部署请换成自己的随机字符串，主从两端须保持一致。
- **数据库密码不要明文写入 `config.ini`**：可在 `url` 中使用 `env:DB_URL` 从环境变量读取连接串。
- **运行期数据**：`master/data/`、`slave/data/`（数据库、日志）以及 `plugins/` 安装目录均已加入 `.gitignore`，不会提交到仓库。

## 插件

AETP 的功能都由插件提供，插件分为三类：

| 类别 | 位置 | 说明 |
|---|---|---|
| 内置插件 | `common/plugins/` | 随平台直装，不可卸载：`appconfig`、`db`、`log`、`comm`、`webapi` |
| 节点插件 | `master/webui/` | 主节点的 Web UI 宿主，安装后即可启用 |
| 预装 / 专属插件 | `master_plugins/`、`slave_plugins/` | 在 `config.ini` 的 `[node] plugins` 中列出即可随启动安装，例如 `heartbeat` |

插件可以在运行时通过插件管理页面（或 `ctx.plugins` 服务）上传 ZIP 包、启用、停用和卸载。插件使用 `plugin.json` 描述元数据，入口由 `entrypoint` 指定。

开发插件的入门与进阶内容见 [docs/开发文档.md](docs/开发文档.md)。

## 许可

本仓库的许可信息待补充。前端依赖的 Sakai 模板许可见 [master/webui/frontend/LICENSE.md](master/webui/frontend/LICENSE.md)。
