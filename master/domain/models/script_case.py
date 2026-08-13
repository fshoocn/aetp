"""领域对象：脚本解析产出的用例索引（P3.2）。

`stable_key` 为跨版本稳定标识（pytest nodeid / CANoe 用例名 / cdd 服务路径），
用于版本 diff 与任务定义勾选引用；avg_duration_s / duration_samples 支撑
by_time 分割（D-21，仅统计成功 case 耗时）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ScriptCase:
    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:script_id 所属脚本业务标识（ULID）
    script_id: str = ""
    # sym:case_id 用例业务标识（ULID），全局唯一，对外暴露
    case_id: str = ""
    # sym:stable_key 脚本内稳定标识，跨版本 diff 与任务定义勾选引用（§18.3）
    stable_key: str = ""
    # sym:name 用例显示名
    name: str = ""
    # sym:parent_path 父路径/分组，用于树形展示
    parent_path: str = ""
    # sym:tags 用例标签，供筛选与分割策略使用
    tags: list[str] = field(default_factory=list)
    # sym:params 用例参数（JSON），插件执行时使用
    params: dict = field(default_factory=dict)
    # sym:avg_duration_s 平均耗时（秒），仅统计成功 case（D-21），by_time 分割依据
    avg_duration_s: float | None = None
    # sym:duration_samples 耗时统计样本数
    duration_samples: int = 0
    # sym:order_index 展示/执行顺序
    order_index: int = 0
    # sym:deleted 软删除标记（版本 diff 失效用例保留引用）
    deleted: bool = False
    # sym:created_at 创建时间（UTC）
    created_at: datetime | None = None
    # sym:updated_at 最后更新时间（UTC）
    updated_at: datetime | None = None
