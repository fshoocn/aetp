# 应用配置（appconfig）

把执行根目录的 `config.ini` 注入为 `ctx.appconfig` 服务，是节点装配时
**最先加载**的基础插件（主从共用、随平台直装）。

## 功能

- **配置读取**：解析 ini 文件为节视图 + 类型化取值（`get_int` / `get_float`
  / `get_bool` / `section` 等）；
- **配置写回**：`update_section(节, {键: 值})` 把改动立即写回 ini，
  外科手术式行编辑——只改点名的键，注释与排版保留；
- **配置注入**：注册 `internal/config` 钩子，声明 `ini_section` 的插件配置
  解析前自动注入对应节值为**默认值层**（显式传入 > ini 节值 > 类默认值）；
- **提交写回**：注册 `internal/commit` 钩子，`fiber.update(...)` 提交时把新配置
  经 `to_ini` 序列化写回对应节（`no_save=True` 跳过）；
- **缺省降级**：配置文件不存在按空配置启动并告警（不阻断节点启动），
  文件存在但非法则启动失败（快速暴露错误）。

## 能力接口

### 服务：`ctx.appconfig`（`provide = "appconfig"`）

| 方法 | 说明 |
|---|---|
| `get(section, key, fallback=None)` | 取原始字符串值 |
| `get_int` / `get_float` / `get_bool` | 类型化取值（带缺省） |
| `section(name)` | 节视图（`get` / `to_dict` 等） |
| `update_section(name, values)` | 更新配置节并立即写回 ini（保留注释） |
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

## 配置注入与写回（`ini_section`）

插件配置类继承 `common.config_base.FieldConfig` 并声明 `ini_section` 后，
即接入上述两个钩子，插件类不必再手写 `from_ini(ctx.appconfig.section(...))`：

```python
class MyConfig(FieldConfig):
    ini_section: ClassVar[str | None] = "web"
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "port": ((int,), "监听端口"),
    }
    port: int = 8080


# 运行时改配置并持久化到 config.ini（部分更新 = 部分写回）
await fiber.ctx.update({"port": 9090})   # → [web] port = 9090
```

未声明 `ini_section` 的插件不参与注入与写回，配置原样透传。
