"""AETP pytest 插件全链路演示脚本。

该脚本故意包含通过、参数化、跳过、失败四类用例，方便验证：
脚本上传 → Master 解析 case → Shard 下发 → Agent 执行 → 日志回传 →
JUnit XML 结果分析 → Master 结果投影。
"""

from __future__ import annotations

import logging
import os
import sys
import time

import pytest

logger = logging.getLogger(__name__)


def test_aetp_smoke_pass():
    """基础通过用例：验证 pytest 能被 Agent 启动。"""
    logger.info("AETP smoke test started")
    print("AETP_LOG smoke: pytest execution is alive", flush=True)
    assert True


@pytest.mark.parametrize("value", [1, 2, 3], ids=["one", "two", "three"])
def test_aetp_parameterized(value: int):
    """参数化用例：验证 case stable key 和多 case 结果解析。"""
    print(f"AETP_LOG parameterized value={value}", flush=True)
    assert value > 0


def test_aetp_environment():
    """环境信息用例：把 Agent 运行环境写入任务日志。"""
    print(f"AETP_LOG python={sys.executable}", flush=True)
    print(f"AETP_LOG cwd={os.getcwd()}", flush=True)
    assert os.getenv("AETP_E2E_EXPECTED", "1") == "1"


def test_aetp_slow_log():
    """慢用例：验证 progress/log 采集和 duration_ms。"""
    for index in range(3):
        print(f"AETP_LOG progress step={index + 1}/3", flush=True)
        time.sleep(0.05)
    assert True


@pytest.mark.skip(reason="AETP E2E：验证 skipped 结果能回传")
def test_aetp_skipped():
    raise AssertionError


@pytest.mark.xfail(reason="AETP E2E：验证 xfail/失败结果分析")
def test_aetp_expected_failure():
    raise AssertionError


@pytest.mark.skipif(
    os.getenv("AETP_E2E_INCLUDE_FAILURE") != "1",
    reason="设置 AETP_E2E_INCLUDE_FAILURE=1 后验证 failed 终态",
)
def test_aetp_expected_failure_toggle():
    print("AETP_LOG intentional failure enabled", flush=True)
    raise AssertionError("intentional failure for AETP result projection test")
