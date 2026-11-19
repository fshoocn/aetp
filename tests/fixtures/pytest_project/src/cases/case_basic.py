"""基础用例：pass / fail / skip / xfail / xpass（junit 六种 status 的覆盖靶）。

非平铺夹具（DESIGN §16.2）：本文件住 ``src/cases/``，组装配方铺排到
``cases/`` 后才可收集——验证 layout 铺排是「能跑」的前提。
"""

from __future__ import annotations

import pytest


def test_pass(greeting: str) -> None:
    """依赖根 conftest 的 fixture——验证 conf/conftest.py 铺排接线。"""
    assert greeting == "hello aetp"


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
