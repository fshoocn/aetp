"""org.example.csv-cases 示例插件测试：兜底 parse_cases 读取资料生成用例。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from plugins.csv_cases.master.executor import create_executor


def test_csv_cases_parse_cases_reads_csv_rows(tmp_path: Path) -> None:
    """上传目录里放一个 CSV，parse_cases 逐行生成用例。"""
    (tmp_path / "cases.csv").write_text(
        "name,level\ntc-boot,1\ntc-shutdown,2\n",
        encoding="utf-8",
    )
    executor = create_executor()
    cases = asyncio.run(executor.parse_cases(str(tmp_path), {}))
    keys = [case["stable_key"] for case in cases]
    assert keys == ["cases.csv::row-1", "cases.csv::row-2"]
    assert cases[0]["name"] == "tc-boot"
    assert cases[0]["parent_path"] == "cases.csv"


def test_csv_cases_parse_cases_rejects_empty_dir(tmp_path: Path) -> None:
    """目录里没有可解析资料时报错。"""
    executor = create_executor()
    try:
        asyncio.run(executor.parse_cases(str(tmp_path), {}))
    except ValueError as exc:
        assert "没有可解析" in str(exc)
    else:  # pragma: no cover - 期望抛错
        raise AssertionError("期望 ValueError")


def test_csv_cases_manifest_declares_ui_entry() -> None:
    """示例插件 manifest 声明了 ui 入口（Executor 自带 UI 形态）。"""
    root = Path(__file__).parents[1] / "plugins" / "csv_cases"
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["point"] == "executor"
    assert manifest["entrypoints"]["ui"] == "ui/index.html"
