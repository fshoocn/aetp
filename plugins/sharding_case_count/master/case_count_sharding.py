"""按每片最大用例数分片的自定义 sharding 插件（独立插件包）。"""

from __future__ import annotations

from aetp_protocol.execution import ShardingRequest, ShardingResult, ShardSpec


class CaseCountSharding:
    """把 cases 按每片最多 target_count 个切分。

    供 TestTask 脚本绑定选择 ``SplitPolicy(type="custom", plugin_id=...)`` 时调用。
    纯分片函数：不改 Run/Shard 状态，只返回分片结果。
    """

    plugin_id = "org.case-count.sharding"
    plugin_version = "1.0.0"

    def split(self, request: ShardingRequest) -> ShardingResult:
        cases = tuple(request.cases)
        policy = request.policy
        if policy.target_count is None or policy.target_count < 1:
            raise ValueError("case-count 分片要求 target_count >= 1")
        keys = tuple(case.stable_key for case in cases)
        chunks = tuple(
            keys[index : index + policy.target_count]
            for index in range(0, len(keys), policy.target_count)
        ) or ((),)
        return ShardingResult(
            shards=tuple(
                ShardSpec(shard_index=index, case_keys=chunk)
                for index, chunk in enumerate(chunks)
            )
        )


def create_sharding() -> CaseCountSharding:
    return CaseCountSharding()


__all__ = ["CaseCountSharding", "create_sharding"]
