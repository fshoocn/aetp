"""参数化用例：parametrize 展开为多条 nodeid（params 近似口径靶）。"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(("value", "expected"), [(1, 1), (2, 4), (3, 9)])
def test_square(value: int, expected: int) -> None:
    assert value * value == expected


@pytest.mark.parametrize("tag", ["smoke", "slow"], ids=["t-smoke", "t-slow"])
def test_tagged(tag: str) -> None:
    assert tag
