"""Master  插件元数据注册表。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from aetp_protocol.ids import PluginId, SemVer
from aetp_protocol.plugin_types import PluginPoint, PluginStatus

from master.domain.models import PluginVersionRecord


class PluginRegistry:
    """只注册已启用且归档存在的  版本，不执行数据库入口字符串。"""

    def __init__(self, archive_root: str | Path) -> None:
        self._archive_root = Path(archive_root).resolve()
        self._records: dict[tuple[str, str, str], PluginVersionRecord] = {}

    def load(self, records: list[PluginVersionRecord]) -> None:
        self._records.clear()
        for record in records:
            self.register(record)

    def register(self, record: PluginVersionRecord) -> None:
        if record.status is not PluginStatus.ENABLED:
            raise ValueError("Master  Registry 只接受 enabled 插件版本")
        archive = Path(record.archive_path).resolve()
        try:
            archive.relative_to(self._archive_root)
        except ValueError as exc:
            raise ValueError("插件归档路径必须位于  archive root 内") from exc
        if not archive.is_file():
            raise ValueError(f"插件归档文件不存在: {archive}")
        archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
        if archive_sha256 != record.archive_sha256.root:
            raise ValueError("插件归档摘要与 Registry 记录不一致")
        if (
            record.manifest.id != record.plugin_id
            or record.manifest.version != record.version
            or record.manifest.point != record.point
        ):
            raise ValueError("插件 Registry 元数据与 Manifest 不一致")
        key = (record.plugin_id.root, record.version.root, record.point.value)
        existing = self._records.get(key)
        if existing is not None and existing.archive_sha256 != record.archive_sha256:
            raise ValueError("同一插件版本 Registry 摘要冲突")
        self._records[key] = record

    def get(
        self,
        plugin_id: PluginId,
        version: SemVer,
        point: PluginPoint,
    ) -> PluginVersionRecord | None:
        return self._records.get((plugin_id.root, version.root, point.value))

    def list(self, point: PluginPoint | None = None) -> tuple[PluginVersionRecord, ...]:
        records = tuple(self._records.values())
        if point is None:
            return records
        return tuple(record for record in records if record.point is point)
