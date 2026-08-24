"""领域对象：测试脚本版本（P3.2）。

脚本文件存 Master 本地 `data/scripts/{script_id}/{version}/`，DB 只存引用与
sha256；同 hash 重复上传幂等复用。config 保持插件配置，hardware_requirements 使用
公共强类型 HardwareRequirements（JSON 仅在持久化边界使用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aetp_protocol.capabilities import HardwareRequirements

from master.domain.enums import ScriptParseLocation, ScriptParseStatus


@dataclass
class TestScript:
    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:project_id 所属项目业务标识（项目边界 D-12）
    project_id: str = ""
    # sym:script_id 脚本业务标识（ULID），全局唯一，对外暴露
    script_id: str = ""
    # sym:task_type 任务类型（插件类型），决定解析与执行插件
    task_type: str = ""
    # sym:name 脚本名（项目内与 version 共同定位版本）
    name: str = ""
    # sym:version 版本号（自 1 递增）
    version: int = 1
    # sym:file_ref 脚本文件引用路径（data/scripts/{script_id}/{version}/）
    file_ref: str = ""
    # sym:size 脚本包字节数
    size: int = 0
    # sym:sha256 内容哈希，同内容幂等复用
    sha256: str = ""
    # sym:config 插件配置 JSON（执行与解析输入）
    config: dict = field(default_factory=dict)
    # sym:hardware_requirements 硬件能力谓词（§18.5 节点匹配）
    hardware_requirements: HardwareRequirements = field(default_factory=HardwareRequirements)
    # sym:parse_status 用例解析状态（pending/parsing/parsed/failed）
    parse_status: ScriptParseStatus = ScriptParseStatus.PENDING
    # sym:parse_location 用例解析执行位置（master/agent，D-17）
    parse_location: ScriptParseLocation = ScriptParseLocation.MASTER
    # sym:result_parse_location 报告解析执行位置（master/agent，D-19）
    result_parse_location: ScriptParseLocation = ScriptParseLocation.MASTER
    # sym:plugin_version 插件版本（Master 校验兼容性）
    plugin_version: str = ""
    # sym:created_by 上传者用户代理主键（审计）
    created_by: int | None = None
    # sym:last_parsed_at 最近一次解析完成时间
    last_parsed_at: datetime | None = None
    # sym:created_at 创建时间（UTC）
    created_at: datetime | None = None
    # sym:updated_at 最后更新时间（UTC）
    updated_at: datetime | None = None


# 防止 pytest 将 TestScript 误识别为测试类（测试文件中会导入本模型）。
# 用 setattr 而非 TestScript.__test__ = False  # type: ignore[reportAttributeAccessIssue]：后者会触发 Pylance 对
# type[TestScript] 属性赋值的类型检查报错；setattr 按字符串名写入类字典，
# 不会被双下划线名称改写（name mangling）干扰，pytest 读取 __dict__ 正常。
TestScript.__test__ = False  # type: ignore[reportAttributeAccessIssue]
