# AETP 内置资源插件包（org.aetp.resource）

一个插件包同时提供 **串口 / 电源 / Vector CAN** 三种资源能力。Agent 安装本包后，
`ResourceProviderResolver` 会把入口工厂产出的三个 Provider 全部注册进
`ResourceProviderRegistry`，能力快照按 provider 聚合上报。

- 插件 ID：`org.aetp.resource`
- 版本：`1.0.0`（SemVer）
- 扩展点：`resource`（`api_version=2.0.0`）
- 装配侧：Agent
- 能力：`resource.serial`、`resource.power`、`resource.can`

## 包内 Provider

| Provider | provider_id | resource_type | 来源 |
|---|---|---|---|
| 串口 | `org.aetp.serial-resource` | `serial` | 功能名→端口映射文件 |
| 电源 | `org.aetp.power-resource` | `power` | 由适配器注入（默认无资源） |
| Vector CAN | `com.vector.can-resource` | `can` | 本机 Vector XL / vxlapi 硬件扫描 |

> provider_id 是资源的归属与路由标识（`resource_id = stable_id(provider_id:...)`），
> 保留真实厂商语义（如 Vector CAN 为 `com.vector.can-resource`）；`manifest.id` 只
> 标识插件包，两者不必相同。多个资源插件包可同时安装、各自提供同类型资源，
> `ResourceProviderRegistry` 按 provider_id / resource_id 区分与路由。

## 目录

```text
aetp_resource/
├── plugin.json          # Manifest V2
├── agent/
│   ├── entry.py         # create_providers() 返回 (serial, power, can) 三个 Provider
│   ├── base.py          # ConfiguredResourceProvider 共享生命周期
│   ├── serial.py        # SerialResourceProvider
│   ├── power.py         # PowerResourceProvider
│   ├── vector_can.py    # VectorCanResourceProvider
│   └── serial_ports.json  # 串口功能名→端口映射（用户可编辑）
└── README.md
```

## 串口映射：固定位置与文件格式

串口 Provider 读取**已安装插件目录**下的固定文件
`{plugin_dir}/org.aetp.resource/{version}/agent/serial_ports.json`，
文件是 UTF-8 JSON 对象：`{ "功能名": "端口号" }`。

示例：

```json
{
  "CAN_Chassis": "COM3",
  "BATT": "COM7"
}
```

- **固定位置**：文件随包分发到 Agent 插件安装目录的 `agent/serial_ports.json`；
  用户自定义时**直接编辑该文件**即可，无需改代码或重打包（升级会随新版本覆盖）。
- **文件格式**：顶层必须是 JSON 对象；键为功能名，值为 Windows 端口名（如 `COM3`）
  或设备路径。发现时会对每个端口做存在性检查：端口存在标 `READY`，不存在标
  `UNAVAILABLE`。文件不存在/非法时串口 Provider 发现为空（不报错，不影响 power/can）。
- 每条串口资源：`resource_id = org.aetp.serial-resource:{功能名}:{端口}`，
  `channel=端口`、`function=功能名`。

> 与旧版 Agent 内置行为的关系：源码内置的串口 Provider 由
> `AETP_AGENT_SERIAL_MAP_FILE` 注入路径；本插件包用包内固定文件，二者都满足
> "用户可自定义"。切换为纯插件装配（去掉源码内置）后以本文件为准。

## 电源 / CAN

- **电源**：默认无资源；需要真实电源能力时由适配层注入发现/控制 hook（本包提供
  空实现骨架，不伪造硬件）。
- **Vector CAN**：从本机 Vector 设备驱动（vxlapi）扫描总线通道生成
  `resource_type=can` 的资源；未安装驱动/无设备时发现为空（不阻塞 Agent）。

## 构建 V2 归档

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File plugins\build_plugin.ps1 -PluginDir plugins\aetp_resource
python plugins/build_plugin.py plugins/aetp_resource
```

产出 `plugins/org.aetp.resource-1.0.0.zip`，内容为 `plugin.json` + `agent/`。

## 装配（过渡态 → 目标）

- 当前（源码内置默认）：`agent/container.py` 直接
  `from plugins.resource_providers import ...` 注册 serial/power/vector_can 三个
  Provider，`ResourceProviderResolver.resolve_all()` 已可从已安装资源插件叠加。
- 目标（纯插件）：`agent/container.py` 去掉源码内置，`ResourceProviderRegistry`
  只由 `ResourceProviderResolver` 从已安装 resource 插件（本包）填充。

## 验证

```powershell
..\..\.venv\Scripts\python.exe -m ruff check plugins/aetp_resource
..\..\.venv\Scripts\python.exe -m pytest tests/test_agent_resource_resolver.py -q
```
