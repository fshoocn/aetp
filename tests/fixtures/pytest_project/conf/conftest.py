"""根 conftest（组装时铺到工程根，验证非平铺布局的 fixture 接线）。"""

from __future__ import annotations

import pytest


@pytest.fixture()
def greeting() -> str:
    return "hello aetp"
