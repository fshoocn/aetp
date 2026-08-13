"""插件加载/查询异常与错误码（P3.8 增强）。

插件随 Master/Agent 分发安装；当 run.assign 下发（Agent）或 Master 派发前
按 task_type 查 registry 时，可能出现插件缺失、版本不兼容、加载失败等
异常。本模块将这些情况建模为显式异常，携带机器可读错误码（§5.5），
由 API/协议层映射为对应响应或 ACK(rejected)。
"""

from __future__ import annotations

# 机器可读错误码（§5.5；PLUGIN_VERSION_MISMATCH 已有，其余为插件加载补充）
PLUGIN_NOT_FOUND = "PLUGIN_NOT_FOUND"
PLUGIN_VERSION_MISMATCH = "PLUGIN_VERSION_MISMATCH"
PLUGIN_LOAD_FAILED = "PLUGIN_LOAD_FAILED"


class PluginError(ValueError):
    """插件相关错误基类（携带机器可读错误码）。"""

    code: str = "PLUGIN_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class PluginNotFoundError(PluginError):
    """task_type 未注册（Agent 端没有该插件 / Master 未注册该任务类型）。

    run.assign 下发到未安装该插件的 Agent 时触发；Agent 应
    ACK(rejected, code=PLUGIN_NOT_FOUND)，Master 记录并可按策略换节点/失败。
    """

    code = PLUGIN_NOT_FOUND


class PluginVersionMismatchError(PluginError):
    """插件版本与声明（run.assign 携带的 plugin_version / supported_versions）不兼容。

    Master 派发前校验不兼容 → 拒绝派发（§18.2）；Agent 加载时声明版本不在
    supported_versions → ACK(rejected, code=PLUGIN_VERSION_MISMATCH)。
    """

    code = PLUGIN_VERSION_MISMATCH


class PluginLoadError(PluginError):
    """插件模块导入/初始化失败（受信任本地加载时异常）。"""

    code = PLUGIN_LOAD_FAILED
