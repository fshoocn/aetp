"""轻量级进程内滑动窗口限流器。

用于登录/注册等敏感接口，缓解暴力破解与注册滥用。
单进程部署下有效；多进程部署需换成共享存储（Redis 等）。
"""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowRateLimiter:
    """按 key（如客户端 IP）统计滑动窗口内请求次数。"""

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """是否允许本次请求；会记录一次命中。"""
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits
            # 清理窗口外记录
            while hits and now - hits[0] > self._window_seconds:
                hits.popleft()
            if len(hits) >= self._max_attempts:
                return False
            hits.append(now)
            return True

    def reset(self, key: str) -> None:
        """清空某个 key 的计数（如登录成功后）。"""
        with self._lock:
            self._hits.pop(key, None)


# 登录：每 IP 60 秒内最多 5 次尝试
login_limiter = SlidingWindowRateLimiter(max_attempts=5, window_seconds=60)
# 注册：每 IP 1 小时内最多 10 次
register_limiter = SlidingWindowRateLimiter(max_attempts=10, window_seconds=3600)


def client_ip(request) -> str:
    """提取客户端 IP（透传 X-Forwarded-For 时取最左端真实地址）。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client is not None else "unknown"
