"""CSV 用例生成器（示例）Agent 面占位执行器。

真实场景：资料类插件在这里读取 Plan 工作目录（Master 下发的原始资料文件 + case_keys）
并执行各自的用例。本示例为演示，逐 case 直接通过，只展示契约（execute + analyze_results）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class CsvCasesAgentExecutor:
    plugin_version = "1.0.0"

    async def execute(self, context) -> Mapping[str, Any]:
        case_keys = list(getattr(context, "case_keys", []) or [])
        await context.progress(0, "csv-cases", "占位执行：逐 case 通过")
        await context.log("info", "占位执行", {"case_keys": case_keys})
        await context.progress(100, "csv-cases", "完成")
        return {
            "status": "succeeded",
            "passed": True,
            "metrics": {"total": len(case_keys), "passed": len(case_keys)},
            "data": {"source": "csv-cases"},
        }


def create_executor() -> CsvCasesAgentExecutor:
    return CsvCasesAgentExecutor()


__all__ = ["CsvCasesAgentExecutor", "create_executor"]
