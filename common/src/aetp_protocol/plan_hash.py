"""V2 ExecutionPlan RFC 8785 规范化摘要。"""

from __future__ import annotations

import hashlib
import json
from typing import cast

import rfc8785

from .execution import ExecutionPlan
from .ids import JsonObject, Sha256


def canonical_plan_document(plan: ExecutionPlan) -> JsonObject:
    """生成参与 hash 的 Plan 语义文档，去除 hash 和临时 URL。"""
    document = cast(JsonObject, json.loads(plan.model_dump_json()))
    document.pop("plan_hash", None)
    document.pop("artifact_upload_url", None)
    for field_name in ("script", "plugin_package"):
        value = document.get(field_name)
        if isinstance(value, dict):
            value.pop("download_url", None)
    return document


def calculate_plan_hash(plan: ExecutionPlan) -> Sha256:
    """按 RFC 8785 对 Plan 语义计算小写 SHA-256。"""
    canonical = rfc8785.dumps(canonical_plan_document(plan))
    return Sha256(hashlib.sha256(canonical).hexdigest())


def with_plan_hash(plan: ExecutionPlan) -> ExecutionPlan:
    """用规范算法填充 Plan hash。"""
    return plan.model_copy(update={"plan_hash": calculate_plan_hash(plan)})


__all__ = ["calculate_plan_hash", "canonical_plan_document", "with_plan_hash"]
