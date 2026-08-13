"""领域对象：测试任务定义（P3.3）。

任务定义与执行分离：`TestTask` 是项目内可复用的定义（引用脚本版本 +
默认勾选用例集合 + 分割/重试策略），一次真实执行是 `task_runs`
（P3.4）。手动/API/定时/重试都只创建新的 Run，不复制或覆盖定义。

引用脚本的**具体版本**（script_id + script_version）：切换版本 =
PATCH 更新 script_ref；被任务定义引用的脚本版本受引用保护不可物理删除。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from master.domain.time import utcnow


@dataclass
class TestTask:
    """测试任务定义。

    split_policy / retry_policy 为结构化 JSON（如
    {"type": "by_time", "target_duration_s": 300}）；
    default_case_selection 为勾选 case 的 stable_key 列表（§18.4，D-15）。
    """

    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:task_id 任务定义业务标识（ULID），全局唯一，对外暴露
    task_id: str = ""
    # sym:project_id 所属项目业务标识（项目边界 D-12）
    project_id: str = ""
    # sym:script_id 引用脚本的业务标识（脚本版本由 script_version 定位）
    script_id: str = ""
    # sym:script_version 引用脚本的版本号（任务定义绑定具体版本）
    script_version: int = 1
    # sym:task_type 任务类型（插件类型），与引用脚本的 task_type 一致
    task_type: str = ""
    # sym:name 定义名，项目内唯一（(project_id, name) 唯一约束）
    name: str = ""
    # sym:default_case_selection 默认勾选用例集合（case 的 stable_key 列表，D-15）；
    #   触发 Run 时可用 case_filter 覆盖，CICD/定时用默认集合
    default_case_selection: list[str] = field(default_factory=list)
    # sym:node_ids 绑定执行节点（业务 ID，⊆ 项目绑定节点，D-23 两层绑定）
    node_ids: list[str] = field(default_factory=list)
    # sym:split_policy 分割策略 JSON：{type: none|by_time|by_case_count|custom, ...}（§18.6）
    split_policy: dict = field(default_factory=dict)
    # sym:max_parallel_shards 任务级最大并行 Shard 数（三层并发上限之一，§18.6）
    max_parallel_shards: int = 1
    # sym:retry_policy 重试策略 JSON：{max_attempts, failover_nodes, case_retry}（D-20）
    retry_policy: dict = field(default_factory=dict)
    # sym:timeout_s 任务超时秒数；0 = 不限制
    timeout_s: int = 0
    # sym:enabled 启用标记：false 时禁止触发新 Run
    enabled: bool = True
    # sym:priority 优先级（数值越大越优先，调度排序用）
    priority: int = 0
    # sym:created_by 创建者（users.id），审计字段
    created_by: int | None = None
    # sym:created_at 创建时间（UTC）
    created_at: datetime | None = None
    # sym:updated_at 最后更新时间（UTC）
    updated_at: datetime | None = None


# 防止 pytest 将 TestTask 误识别为测试类（测试文件中会导入本模型）。
# 用 setattr 按字符串名写入类字典，避免双下划线名称改写（name mangling），
# 且不会触发 Pylance 对 type[TestTask] 属性赋值的类型检查报错。
setattr(TestTask, "__test__", False)
