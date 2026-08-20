"""ULID 业务标识生成（D-11，§5.1）。

D-11 明确：外部业务 ID 采用 ULID——使用**非单调、密码学安全随机数**
生成器；ULID 仅用于标识和排序，绝不作为授权或访问凭证。

本实现仅依赖标准库（§4.6 允许范围）：
- 时间戳段：48-bit 毫秒时间戳（Crockford Base32 前 10 字符），提供
  粗略的时间排序性；
- 随机段：80-bit `secrets.token_bytes`（密码学安全随机），保证不可预测、
  不可枚举——这是 D-11「非单调、密码学安全随机」的核心。

ULID 标准格式为 26 字符 Crockford Base32
（``0123456789ABCDEFGHJKMNPQRSTVWXYZ``，排除易混淆的 I/L/O/U），
不含前缀与分隔符，可直接作为 URL 段 / 目录名 / 协议标识使用。
"""

from __future__ import annotations

import secrets
import time

# Crockford Base32 字母表（排除 I、L、O、U，避免视觉混淆）
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# ULID 时间戳段与随机段长度（字符数）
_TIMESTAMP_CHARS = 10
_RANDOM_CHARS = 16


def _encode(value: int, length: int) -> str:
    """把非负整数编码为定长 Crockford Base32 字符串（高位补 0 字符）。"""
    chars: list[str] = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid() -> str:
    """生成 26 字符 ULID（48-bit 毫秒时间戳 + 80-bit 密码学安全随机）。"""
    timestamp_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF  # 48-bit
    random_int = int.from_bytes(secrets.token_bytes(10), "big")  # 80-bit
    return _encode(timestamp_ms, _TIMESTAMP_CHARS) + _encode(
        random_int, _RANDOM_CHARS
    )


# 别名：业务标识统一入口（D-11：外部业务 ID 一律纯 ULID）
new_id = new_ulid
