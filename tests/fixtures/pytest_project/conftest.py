"""根 conftest：``greeting`` fixture 接线（平铺工程根，收集与执行共用）。"""

from __future__ import annotations

import pytest


@pytest.fixture()
def greeting() -> str:
    return "hello aetp"
