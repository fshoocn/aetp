# AETP 项目铁律与提交纪律

## 架构铁律（不可违反）

**插件不能直接依赖插件，插件之间必须通过 cordis 调用。**

- 插件 A 不得 `import` / 实例化 / 直接调用插件 B 的类、函数、模块；
- 插件间一切运行时交互经 cordis 机制：`ctx.<service>`（服务调用）、
  `ctx.on / ctx.emit`（事件）、`inject` 声明依赖；
- 共享的契约类型 / 常量若需跨插件使用，应放在**非插件的公共层**
  （如 `common/`），或经服务实例暴露，不得形成插件包之间的 import；
- 审查口令：在任何插件包里看到 `from common.plugins.<别的插件>`、
  `from master_plugins.<别的插件>`、`from slave_plugins.<别的插件>`
  即为违规（同包内 `from .xxx` 不算）；
- **边界裁决（严格）**：**db 也不豁免**——所有插件必须经 `inject` 声明依赖的
  地基插件，**必须通过 cordis 调用服务（`ctx.<service>`）**，一律不得
  `import` 其他插件包（含 db 的 Model DSL / 异常 / 存储类）；共享契约与
  工具一律放 `common/` 公共层（如 `common.models`、`common.db_registry`、
  `common.db_errors`、`common.contracts`、`common.capabilities`、
  `common.log_op`），插件包内只留再导出垫片维持公开导入路径。

## 提交纪律

- `docs/` 下的文档**没有用户明确指令不得提交**；
- 提交信息**一律不加** `Co-authored-by: Copilot ...` 尾注；
- 历史已推送的提交如需清理尾注 / 改写，须经用户确认后强推。
