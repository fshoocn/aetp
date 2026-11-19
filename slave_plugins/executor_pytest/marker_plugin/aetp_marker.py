"""aetp_marker：pytest 极小插件——case 哨兵标记 + 结构化收集清单。

随 executor_pytest 插件包分发，由执行端以前插 ``PYTHONPATH`` + ``-p aetp_marker``
注入子进程。两个职责（DESIGN §7 / §10.4）：

1. **运行期哨兵**：``pytest_runtest_logstart`` / ``pytest_runtest_logfinish``
   向 stdout 打哨兵行 ``\\x00AETP_CASE <nodeid>\\x00`` 与
   ``\\x00AETP_CASE_END <nodeid>\\x00``——stream.py 据此做 LogChunk 的 case
   归属，并作为 junit 解析 case_id 的首选 nodeid 来源；
2. **收集清单**：环境变量 ``AETP_MARKER_OUT`` 非空时（collect / plan 模式），
   ``pytest_collection_modifyitems`` 把结构化用例清单导出为 JSON：

   ``{"cases": [{"case_id", "name", "path", "markers", "params"}]}``

   ``params`` 是**从 nodeid 解析的近似值**（parametrize 原始参数值只存在
   对象内部，nodeid 里是字符串化 id；口径如实声明，以 nodeid 为准）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

#: 收集清单导出路径（环境变量；空 = 不导出）
OUT_ENV = "AETP_MARKER_OUT"
#: 哨兵：logstart 切换当前用例（与 stream.py 同源）
CASE_MARKER = "\x00AETP_CASE "
#: 哨兵：logfinish 归零当前用例
CASE_END_MARKER = "\x00AETP_CASE_END "
#: 不进 markers 清单的内置 plumbing 标记
_HIDDEN_MARKERS: frozenset[str] = frozenset({"parametrize"})


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int, str | None]) -> None:
    """用例开始：打哨兵行切换 case 归属。"""
    print(f"{CASE_MARKER}{nodeid}\x00")
    sys.stdout.flush()


def pytest_runtest_logfinish(nodeid: str, location: tuple[str, int, str | None]) -> None:
    """用例结束：打哨兵行归零 case 归属。"""
    print(f"{CASE_END_MARKER}{nodeid}\x00")
    sys.stdout.flush()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """collect 模式：导出结构化用例清单到 ``AETP_MARKER_OUT``。"""
    target = os.environ.get(OUT_ENV, "").strip()
    if not target:
        return
    cases: list[dict[str, Any]] = []
    root = Path(config.rootpath)
    for item in items:
        cases.append(_describe(item, root))
    Path(target).write_text(
        json.dumps({"cases": cases}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def _describe(item: pytest.Item, root: Path) -> dict[str, Any]:
    """单条用例的结构化描述（case_id = nodeid，params 为 nodeid 近似值）。"""
    nodeid = item.nodeid
    try:
        path = str(item.path.relative_to(root).as_posix())
    except ValueError:
        path = str(item.path.as_posix())
    markers = sorted({marker.name for marker in item.own_markers if marker.name not in _HIDDEN_MARKERS})
    return {
        "case_id": nodeid,
        "name": item.name,
        "path": path,
        "markers": markers,
        "params": _params_from_nodeid(nodeid),
    }


def _params_from_nodeid(nodeid: str) -> dict[str, str]:
    """从 nodeid 的 ``[...]`` 后缀解析参数近似值（DESIGN §7 口径）。"""
    name = nodeid.rsplit("::", 1)[-1]
    start, end = name.find("["), name.rfind("]")
    if start < 0 or end <= start:
        return {}
    return {"id": name[start + 1 : end]}
