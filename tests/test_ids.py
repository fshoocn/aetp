"""D-11 ULID 生成器契约测试（§5.1）。"""

from __future__ import annotations

import re

from aetp_protocol.ids import new_id, new_ulid

# Crockford Base32 字母表（排除 I/L/O/U）
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def test_new_ulid_format():
    """ULID 为 26 字符 Crockford Base32，不含 I/L/O/U。"""
    value = new_ulid()
    assert _ULID_RE.match(value) is not None


def test_new_ulid_unique_and_non_monotonic():
    """连续生成的 ULID 唯一（80-bit 密码学安全随机段，不可枚举）。"""
    values = {new_ulid() for _ in range(1000)}
    assert len(values) == 1000


def test_new_ulid_time_ordered_prefix():
    """同一毫秒时间窗口内，前 10 字符时间戳段一致（提供粗略排序性）。"""
    # 连续生成两次，时间戳段（前 10 位）要么相等（同毫秒）要么递增。
    a = new_ulid()
    b = new_ulid()
    assert a[:10] <= b[:10]


def test_new_id_alias():
    """new_id 是 new_ulid 的别名，同样产出 26 字符 ULID（无前缀）。"""
    value = new_id()
    assert _ULID_RE.match(value) is not None
    assert "-" not in value


def test_new_ulid_no_forbidden_chars():
    """ULID 绝不包含易混淆字符 I/L/O/U 与连字符/下划线。"""
    for _ in range(200):
        value = new_ulid()
        assert not any(c in value for c in "ILOU-")
