"""基础用例：pass / fail / skip / xfail / xpass（junit 六 status 的覆盖靶）。

平铺夹具工程（与 examples/pytest_demo 同构）：根 conftest.py 的 ``greeting``
fixture 与 ``helpers`` 跨包 import 均直连可跑，无需任何组装铺排。
"""

from __future__ import annotations

import pytest
from helpers import add


def test_pass(greeting: str) -> None:
    """依赖根 conftest 的 fixture + helpers 跨包 import。"""
    assert greeting == "hello aetp"
    assert add(1, 2) == 3


def test_fail() -> None:
    assert 1 == 2


@pytest.mark.skip(reason="fixture skip")
def test_skip() -> None:
    raise AssertionError("不应执行")


@pytest.mark.xfail(reason="known bug")
def test_xfail() -> None:
    assert False


@pytest.mark.xfail(reason="maybe fixed")
def test_xpass() -> None:
    assert True


@pytest.mark.xfail(reason="strict", strict=True)
def test_xpass_strict() -> None:
    assert True
