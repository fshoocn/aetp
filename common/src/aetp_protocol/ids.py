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
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator
from typing_extensions import TypeAliasType

# Crockford Base32 字母表（排除 I、L、O、U，避免视觉混淆）
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# ULID 时间戳段与随机段长度（字符数）
_TIMESTAMP_CHARS = 10
_RANDOM_CHARS = 16


class BusinessId(RootModel[str]):
  """V2 业务 ID：26 字符 Crockford Base32 ULID。"""

  root: str = Field(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")


class PluginId(RootModel[str]):
  """插件稳定标识。"""

  root: str = Field(pattern=r"^[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)+$")


class CapabilityName(RootModel[str]):
  """能力名称，至少包含 domain.action 两段。"""

  root: str = Field(pattern=r"^[a-z][a-z0-9-]*(?:\.[a-z0-9-]+){1,3}$")


class SessionId(RootModel[str]):
  root: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class MessageId(RootModel[str]):
  root: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class RequestId(RootModel[str]):
  root: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class TraceId(RootModel[str]):
  root: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class Sha256(RootModel[str]):
  root: str = Field(pattern=r"^[a-fA-F0-9]{64}$")

  @field_validator("root")
  @classmethod
  def normalize(cls, value: str) -> str:
    return value.lower()


class SemVer(RootModel[str]):
  root: str = Field(
    pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
  )


class Version(RootModel[str]):
  root: str = Field(pattern=r"^v?\d+(?:\.\d+)*$")


class VersionConstraint(BaseModel):
  model_config = ConfigDict(extra="forbid", frozen=True)

  exact: Version | None = None
  minimum: Version | None = None
  maximum: Version | None = None

  @model_validator(mode="after")
  def validate_presence(self) -> VersionConstraint:
    if self.exact is None and self.minimum is None and self.maximum is None:
      raise ValueError("version constraint must contain exact, minimum or maximum")
    return self


class VersionRange(BaseModel):
  model_config = ConfigDict(extra="forbid", frozen=True)

  exact: SemVer | None = None
  minimum: SemVer | None = None
  maximum: SemVer | None = None

  @model_validator(mode="after")
  def validate_presence(self) -> VersionRange:
    if self.exact is None and self.minimum is None and self.maximum is None:
      raise ValueError("version range must contain exact, minimum or maximum")
    return self


class RelativePath(RootModel[str]):
  root: str = Field(pattern=r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")

  @field_validator("root")
  @classmethod
  def validate_relative(cls, value: str) -> str:
    if value.startswith("/") or "\\" in value or any(part == ".." for part in value.split("/")):
      raise ValueError("path must be relative and cannot contain parent traversal")
    return value


JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue = TypeAliasType("JsonValue", JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"])
JsonObject: TypeAlias = dict[str, JsonValue]


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
    return _encode(timestamp_ms, _TIMESTAMP_CHARS) + _encode(random_int, _RANDOM_CHARS)


# 别名：业务标识统一入口（D-11：外部业务 ID 一律纯 ULID）
new_id = new_ulid
