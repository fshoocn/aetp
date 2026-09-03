# CANoe Software Discovery（AETP V2 独立插件包）

software 环境发现插件：上报本机 CANoe 商业软件能力（版本 + License 状态）。
Provider 拥有 `name=CANoe`，安装后基础 PATH 探测不再重复上报 CANoe。

- 插件 ID：`org.aetp.canoe-software`
- 版本：`1.0.0`（SemVer）
- 扩展点：`software`（`api_version=2.0.0`）
- 装配侧：Agent
- 能力：`software.discovery.canoe`
- Provider：`provider_id=org.aetp.canoe-software`、`name=CANoe`

## 目录

```text
canoe_software/
├── plugin.json
├── agent/
│   ├── canoe_software.py   # CanoeSoftwareProvider（读映射文件）
│   └── entry.py            # create_providers()
└── README.md
```

## 插件契约

- 工厂 `create_providers()` 返回 `SoftwareProvider` 元组（本包一个）。
- Provider 实现 `aetp_protocol.discovery.SoftwareProvider`：声明
  `provider_id`/`name`，`discover() -> tuple[SoftwareCapability, ...]`。
- 插件**只依赖 aetp_protocol**；不触碰 Agent Kernel。

## License 探测说明

License 状态无法在进程内可靠探测，因此默认从**已安装插件**目录下固定文件
`agent/canoe_software.json` 读取：

```json
{ "version": "17.0", "license_available": true }
```

- 文件不存在或无效时发现为空（不报能力、不伪造）。
- 编辑该文件即可自定义版本与 License 可用性；Master 用
  `license_available=true` 校验 `static_requirements.software` 的
  `license_required` 项。
- 真实部署可用自有探测逻辑替换本 Provider（构造时注入 `discoverer`）。

## 构建

```powershell
python plugins/build_plugin.py plugins/canoe_software
```

产出 `plugins/org.aetp.canoe-software-1.0.0.zip`。
