"""CSV 用例生成器（示例）Master 面。

资料类 executor 示例：用例通常由插件 UI 在浏览器端生成并经上传请求直接携带
（``cases`` 表单字段），Master 收到后直接落库、不调用本 ``parse_cases``。本方法仅作
为兜底：若调用方未携带 cases（例如直接经 API 上传），则读取上传目录里的 CSV 逐行
生成用例，保证两条链路一致。

只依赖 aetp_protocol，不触碰 Kernel。
"""

from __future__ import annotations

import csv
from collections.abc import Mapping
from pathlib import Path

from aetp_protocol.execution import ExecutionPlan  # noqa: F401  # 仅供类型提示参考
from aetp_protocol.ids import PluginId, SemVer  # noqa: F401


class CsvCasesMasterExecutor:
    plugin_id = "org.example.csv-cases"
    plugin_version = "1.0.0"

    async def parse_cases(
        self,
        script_dir: str | Path,
        configuration: Mapping[str, object],
    ) -> tuple[dict[str, str], ...]:
        """把上传目录里的 CSV/文本资料逐行解析为用例（stable_key = 行内容摘要）。"""
        del configuration
        root = Path(script_dir).resolve()
        sources = sorted(
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in {".csv", ".txt", ".tsv"}
        )
        if not sources:
            raise ValueError("CSV 用例生成器：目录里没有可解析的 .csv/.txt/.tsv 资料")
        rows: list[dict[str, str]] = []
        for source in sources:
            rows.extend(_rows_from(source))
        if not rows:
            raise ValueError("CSV 用例生成器：资料里没有可用的数据行")
        cases: list[dict[str, str]] = []
        for index, row in enumerate(rows, start=1):
            stable_key = f"{sources[0].name}::row-{index}"
            cases.append(
                {
                    "stable_key": stable_key,
                    "name": row.get("name") or f"row-{index}",
                    "parent_path": sources[0].name,
                }
            )
        return tuple(cases)


def _rows_from(path: Path) -> list[dict[str, str]]:
    """按文件类型把内容拆成行映射（CSV 用表头；纯文本一行一个）。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".csv":
        reader = csv.DictReader(text.splitlines())
        return [dict(row) for row in reader if any((value or "").strip() for value in row.values())]
    return [{"name": line.strip()} for line in text.splitlines() if line.strip()]


def create_executor() -> CsvCasesMasterExecutor:
    return CsvCasesMasterExecutor()


__all__ = ["CsvCasesMasterExecutor", "create_executor"]
