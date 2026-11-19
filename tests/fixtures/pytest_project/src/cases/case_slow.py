"""慢用例：sleep 靶（取消 / 超时验证用）。

``test_sleep_with_child`` 额外孵一个 multiprocessing 子进程并把 PID 落到
cwd 下的 ``child_pid.txt``——取消时杀**进程树**的验证靶（决策 D3）：
若取消后该子进程仍存活，即杀树失效。
"""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path


def test_sleep() -> None:
    time.sleep(120.0)


def test_sleep_with_child() -> None:
    child = multiprocessing.Process(target=time.sleep, args=(120.0,))
    child.start()
    Path("child_pid.txt").write_text(str(child.pid), encoding="utf-8")
    time.sleep(120.0)
