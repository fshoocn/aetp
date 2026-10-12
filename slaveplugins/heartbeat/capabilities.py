"""能力信封契约：让各插件以统一「信封 + 不透明载荷」挂载能力信息。

「格式不确定」问题的解法：**会被查询 / 筛选的字段必须同构（信封），
仅供展示的数据保持自由**。消费方（主模块）只解析信封，``detail``
原样透传，解析成本与 ``detail`` 复杂度无关。

信封契约（协议详见 ``masterplugins/heartbeat/DESIGN.md`` §2 / §3）::

    {
      "kind": "executor",            # 必填：能力大类（筛选主键）
      "id": "pytest",                # 必填：能力标识（节点内唯一）
      "provider": "pytest-runner",   # 必填：提供者插件名
      "version": "8.3.4",            # 可选
      "labels": {"lang": "python"},  # 可选：自由标签（筛选轴）
      "summary": "Python 单测执行",   # 可选：一句话描述
      "detail": {...}                # 可选：任意 JSON，消费方不解析
    }

约定：

* 未知信封字段忽略（向前兼容）；新增字段只加可选项；
* 坏条目（缺必填 / 无法 JSON 序列化）在收集端就地丢弃并上报问题，
  不打挂心跳；
* 摘要（:func:`capabilities_digest`）与条目顺序无关，供心跳按需同步
  （平时只发 ``caps_digest``，变了 / 主模块要时才发全量）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import cast

from cordis_port import Context, EventDispatchError

#: 能力收集事件：把**能力条目列表**传给监听器，插件往里 ``append`` 信封
#: 条目（推荐用 :func:`capability` 构造）。并发派发、单个贡献者出错不影响
#: 其余贡献。
CAP_EVENT = "heartbeat/collect-capabilities"


@dataclass(frozen=True)
class Capability:
    """一条能力的信封（``detail`` 不解析，原样保留）。"""

    #: 能力大类（筛选主键），如 ``executor`` / ``driver`` / ``reporter``
    kind: str
    #: 能力标识（节点内唯一），如 ``pytest``
    id: str
    #: 提供者插件名
    provider: str
    #: 版本串（可选）
    version: str = ""
    #: 自由标签（可选），筛选轴，如 ``{"lang": "python"}``
    labels: Mapping[str, str] = field(default_factory=dict[str, str])
    #: 一句话描述（可选），列表展示 / 全文搜索用
    summary: str = ""
    #: 任意 JSON（可选），消费方不解析，展示层用通用 JSON 树
    detail: object = None

    def to_dict(self) -> dict[str, object]:
        """转回线上信封形态（字段齐全，供存储 / 序列化）。"""
        return {
            "kind": self.kind,
            "id": self.id,
            "provider": self.provider,
            "version": self.version,
            "labels": dict(self.labels),
            "summary": self.summary,
            "detail": self.detail,
        }


def capability(
    kind: str,
    cap_id: str,
    provider: str,
    *,
    version: str = "",
    labels: Mapping[str, str] | None = None,
    summary: str = "",
    detail: object = None,
) -> dict[str, object]:
    """构造一条信封条目（供 :data:`CAP_EVENT` 监听器 ``append``）。

    ``cap_id`` 对应信封的 ``id`` 字段；空的可选字段不占位。
    """
    entry: dict[str, object] = {"kind": kind, "id": cap_id, "provider": provider}
    if version:
        entry["version"] = version
    if labels:
        entry["labels"] = dict(labels)
    if summary:
        entry["summary"] = summary
    if detail is not None:
        entry["detail"] = detail
    return entry


def canonical_json(value: object) -> str:
    """键排序的紧凑 JSON 序列化——摘要的规范形态，与键序无关。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_capability(raw: object) -> Capability | None:
    """把线上条目解析成 :class:`Capability`；信封不合格返回 ``None``。

    只严格校验必填项（``kind`` / ``id`` / ``provider`` 非空串）；可选字段
    缺失给默认值、类型不对降级为空（不整条丢弃）；未知字段忽略。
    """
    if not isinstance(raw, dict):
        return None
    entry = cast(dict[str, object], raw)
    kind = entry.get("kind")
    cap_id = entry.get("id")
    provider = entry.get("provider")
    if not (
        isinstance(kind, str)
        and kind
        and isinstance(cap_id, str)
        and cap_id
        and isinstance(provider, str)
        and provider
    ):
        return None
    labels_raw = entry.get("labels")
    labels: dict[str, str] = {}
    if isinstance(labels_raw, dict):
        labels = {
            str(key): str(value)
            for key, value in cast(dict[object, object], labels_raw).items()
        }
    version = entry.get("version")
    summary = entry.get("summary")
    return Capability(
        kind=kind,
        id=cap_id,
        provider=provider,
        version=version if isinstance(version, str) else "",
        labels=labels,
        summary=summary if isinstance(summary, str) else "",
        detail=entry.get("detail"),
    )


def capabilities_digest(entries: Iterable[Mapping[str, object]]) -> str:
    """能力列表的稳定摘要（16 位 hex）：条目顺序不影响结果。

    用于心跳的按需同步（载荷 ``caps_digest`` 与主模块存档比对）。
    条目须是 JSON 安全的（:func:`collect_capabilities` 的返回值已保证）。
    """
    canonical = sorted(canonical_json(entry) for entry in entries)
    return hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest()[:16]


def select_capabilities(
    caps: Iterable[Capability],
    *,
    kinds: Collection[str] | None = None,
    labels: Mapping[str, str] | None = None,
    q: str | None = None,
) -> list[Capability]:
    """按 ``kind`` / ``labels`` 子集 / 自由文本筛选能力（主模块筛选 API 用）。

    * ``kinds``：命中任一即保留；
    * ``labels``：给定标签是条目标签的**子集**才保留（多标签取交）；
    * ``q``：大小写不敏感命中 ``id`` 或 ``summary``。
    """
    kind_set = set(kinds) if kinds is not None else None
    needle = q.casefold() if q else None
    result: list[Capability] = []
    for cap in caps:
        if kind_set is not None and cap.kind not in kind_set:
            continue
        if labels is not None and not all(
            cap.labels.get(key) == value for key, value in labels.items()
        ):
            continue
        if needle is not None and needle not in f"{cap.id} {cap.summary}".casefold():
            continue
        result.append(cap)
    return result


def _preview(raw: object) -> str:
    """条目预览（问题描述用，截断防日志爆炸）。"""
    text = repr(raw)
    return text if len(text) <= 100 else f"{text[:97]}..."


async def collect_capabilities(
    ctx: Context,
) -> tuple[list[dict[str, object]], list[str]]:
    """派发 :data:`CAP_EVENT` 收集能力条目，返回 ``(合规条目, 问题描述)``。

    * 并发派发，单个贡献者出错不影响其余（错误并入问题列表）；
    * 条目须通过信封校验且可 JSON 序列化（否则整拍心跳都会序列化失败），
      不合格的就地丢弃并给出问题描述；
    * 返回条目经 JSON 往返归一化并按规范序排序，摘要跨拍稳定。
    """
    bucket: list[object] = []
    problems: list[str] = []
    try:
        await ctx.parallel(CAP_EVENT, bucket)
    except EventDispatchError as exc:
        problems.extend(
            f"贡献者抛错：{type(error).__name__}: {error}" for error in exc.errors
        )
    entries: list[dict[str, object]] = []
    for raw in bucket:
        if not isinstance(raw, dict):
            problems.append(f"能力条目不是对象，已丢弃：{_preview(raw)}")
            continue
        # isinstance 收窄裸 dict 得 dict[Unknown, Unknown]，显式 cast 定型
        entry = cast(dict[str, object], raw)
        if parse_capability(entry) is None:
            problems.append(f"能力条目不合信封契约，已丢弃：{_preview(entry)}")
            continue
        try:
            normalized = cast(
                dict[str, object], json.loads(json.dumps(entry, ensure_ascii=False))
            )
        except (TypeError, ValueError, RecursionError):
            problems.append(f"能力条目无法 JSON 序列化，已丢弃：{_preview(entry)}")
            continue
        entries.append(normalized)
    entries.sort(key=canonical_json)
    return entries, problems
