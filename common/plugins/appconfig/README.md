# 应用配置（appconfig）

把执行根目录的 `config.ini` 注入为 `ctx.appconfig` 服务，是节点装配时
**最先加载**的基础插件（主从共用、随平台直装）。

## 功能

- **配置读取**：解析 ini 文件为节视图 + 类型化取值（`get_int` / `get_float`
  / `get_bool` / `section` 等）；
- **缺省降级**：配置文件不存在按空配置启动并告警（不阻断节点启动），
  文件存在但非法则启动失败（快速暴露错误）。

## 能力接口

### 服务：`ctx.appconfig`（`provide = "appconfig"`）

| 方法 | 说明 |
|---|---|
| `get(section, key, fallback=None)` | 取原始字符串值 |
| `get_int` / `get_float` / `get_bool` | 类型化取值（带缺省） |
| `section(name)` | 节视图（`get` / `to_dict` 等） |
| `path_loaded` | 实际加载的配置文件路径 |

## 使用方法

插件声明 `inject = ["appconfig"]` 即可取用：

```python
class MyPlugin:
    inject = ["appconfig"]

    def __init__(self, ctx, config):
        port = ctx.appconfig.get_int("web", "port", 8080)
```

配置文件路径由装配层传入（`common/node_runtime.py` 按 config.ini 所在
目录装配），业务插件不需要关心加载细节。
