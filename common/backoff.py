"""指数退避（带抖动）（P4.2，§9.7 规则 5，Master/Agent 共享）。

重连/重试用：延迟随尝试次数指数增长并封顶，加比例抖动避免
多个客户端同时重连（惊群）。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class ExponentialBackoff:
    """指数退避计算器（可测试的纯逻辑）。"""

    # sym:base_delay_s 基础延迟（秒）
    base_delay_s: float = 1.0
    # sym:max_delay_s 最大延迟（秒，封顶）
    max_delay_s: float = 60.0
    # sym:factor 退避因子（每次乘）
    factor: float = 2.0
    # sym:jitter_ratio 抖动比例（±比例）
    jitter_ratio: float = 0.1
    # sym:_attempts 已尝试次数（内部计数）
    _attempts: int = field(default=0, repr=False)

    def next(self) -> float:
        """返回下一次等待秒数（递增）并推进计数。"""
        raw = min(self.base_delay_s * (self.factor**self._attempts), self.max_delay_s)
        self._attempts += 1
        jitter = random.uniform(-self.jitter_ratio, self.jitter_ratio)
        return max(0.0, raw * (1.0 + jitter))

    def reset(self) -> None:
        """连接成功后重置计数。"""
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts
