"""Master  插件版本治理应用服务。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from aetp_protocol.ids import PluginId, SemVer, Sha256
from aetp_protocol.plugin_archive import PluginArchiveVerifier
from aetp_protocol.plugin_types import PluginRef, PluginStatus

from master.domain.models import PluginVersionRecord
from master.domain.repositories import UnitOfWork
from master.plugins.lifecycle import assert_transition


class PluginGovernanceService:
    """管理已验证插件归档和需重启生效的版本状态。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        archive_root: Path,
        verifier: PluginArchiveVerifier | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._archive_root = archive_root
        self._verifier = verifier or PluginArchiveVerifier()

    def register_archive(self, filename: str, content: bytes) -> PluginVersionRecord:
        verified = self._verifier.verify(content, filename=filename)
        manifest_bytes = json.dumps(
            verified.manifest.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        manifest_sha256 = Sha256(hashlib.sha256(manifest_bytes).hexdigest())
        archive_path = self._archive_path(verified.manifest.id, verified.manifest.version, verified.sha256)

        with self._uow_factory() as uow:
            existing = uow.plugin_versions.get(verified.manifest.id, verified.manifest.version)
            if existing is not None:
                if existing.archive_sha256 != verified.sha256:
                    raise ValueError("插件版本已存在但 SHA-256 不同")
                return existing

            wrote_archive = self._write_immutable_archive(archive_path, content)
            try:
                return uow.plugin_versions.add(
                    PluginVersionRecord(
                        id=None,
                        plugin_id=verified.manifest.id,
                        version=verified.manifest.version,
                        point=verified.manifest.point,
                        status=PluginStatus.VERIFIED,
                        filename=verified.filename,
                        archive_sha256=verified.sha256,
                        manifest_sha256=manifest_sha256,
                        manifest=verified.manifest,
                        archive_path=str(archive_path),
                        installed_at=None,
                        created_at=None,
                        updated_at=None,
                    )
                )
            except Exception:
                if wrote_archive:
                    archive_path.unlink(missing_ok=True)
                raise

    def list_versions(self, *, plugin_id: PluginId | None = None) -> list[PluginVersionRecord]:
        with self._uow_factory() as uow:
            records = uow.plugin_versions.list()
        if plugin_id is None:
            return records
        return [record for record in records if record.plugin_id == plugin_id]

    def install(self, plugin_id: PluginId, version: SemVer) -> PluginVersionRecord:
        with self._uow_factory() as uow:
            record = self._require(uow, plugin_id, version)
            assert_transition(record.status, PluginStatus.INSTALLED)
            return uow.plugin_versions.update(
                replace(record, status=PluginStatus.INSTALLED, installed_at=record.installed_at or datetime.now(UTC))
            )

    def request_enabled(self, plugin_id: PluginId, version: SemVer) -> PluginVersionRecord:
        with self._uow_factory() as uow:
            record = self._require(uow, plugin_id, version)
            if record.status not in {PluginStatus.INSTALLED, PluginStatus.DISABLED}:
                raise ValueError(f"插件版本当前不可启用: {record.status.value}")
            assert_transition(record.status, PluginStatus.PENDING_RESTART)
            return uow.plugin_versions.update(replace(record, status=PluginStatus.PENDING_RESTART))

    def request_disabled(self, plugin_id: PluginId, version: SemVer) -> PluginVersionRecord:
        with self._uow_factory() as uow:
            record = self._require(uow, plugin_id, version)
            assert_transition(record.status, PluginStatus.PENDING_RESTART)
            return uow.plugin_versions.update(replace(record, status=PluginStatus.PENDING_RESTART))

    def complete_restart(
        self,
        plugin_id: PluginId,
        version: SemVer,
        *,
        enabled: bool,
    ) -> PluginVersionRecord:
        target = PluginStatus.ENABLED if enabled else PluginStatus.DISABLED
        pointer_path = self._active_path(plugin_id)
        previous_pointer = pointer_path.read_bytes() if pointer_path.exists() else None
        pointer_changed = False
        try:
            with self._uow_factory() as uow:
                record = self._require(uow, plugin_id, version)
                previous = self._active_record(uow, plugin_id)
                if (
                    enabled
                    and previous is not None
                    and previous.id != record.id
                    and previous.status is PluginStatus.ENABLED
                ):
                    assert_transition(previous.status, PluginStatus.PENDING_RESTART)
                    previous = uow.plugin_versions.update(
                        replace(previous, status=PluginStatus.PENDING_RESTART)
                    )
                    assert_transition(previous.status, PluginStatus.DISABLED)
                    uow.plugin_versions.update(replace(previous, status=PluginStatus.DISABLED))
                assert_transition(record.status, target)
                updated = uow.plugin_versions.update(replace(record, status=target))
                if enabled:
                    self._write_active_pointer(pointer_path, PluginRef(
                        plugin_id=plugin_id,
                        version=version,
                        archive_sha256=updated.archive_sha256,
                    ).model_dump_json())
                    pointer_changed = True
                elif pointer_path.exists() and self._pointer_matches(pointer_path, plugin_id, version):
                    pointer_path.unlink()
                    pointer_changed = True
                return updated
        except Exception:
            if pointer_changed:
                self._restore_active_pointer(pointer_path, previous_pointer)
            raise

    def rollback(self, plugin_id: PluginId, version: SemVer) -> PluginVersionRecord:
        """切换 active pointer 到已安装的指定版本，并停用当前版本。"""
        pointer_path = self._active_path(plugin_id)
        previous_pointer = pointer_path.read_bytes() if pointer_path.exists() else None
        pointer_changed = False
        try:
            with self._uow_factory() as uow:
                target = self._require(uow, plugin_id, version)
                if target.status not in {PluginStatus.INSTALLED, PluginStatus.DISABLED, PluginStatus.ENABLED}:
                    raise ValueError(f"插件版本当前不可回滚: {target.status.value}")
                current = self._active_record(uow, plugin_id)
                if current is not None and current.id == target.id:
                    return target
                if current is not None and current.status is PluginStatus.ENABLED:
                    assert_transition(current.status, PluginStatus.PENDING_RESTART)
                    current = uow.plugin_versions.update(
                        replace(current, status=PluginStatus.PENDING_RESTART)
                    )
                    assert_transition(current.status, PluginStatus.DISABLED)
                    uow.plugin_versions.update(replace(current, status=PluginStatus.DISABLED))
                if target.status in {PluginStatus.INSTALLED, PluginStatus.DISABLED}:
                    assert_transition(target.status, PluginStatus.PENDING_RESTART)
                    target = uow.plugin_versions.update(
                        replace(target, status=PluginStatus.PENDING_RESTART)
                    )
                assert_transition(target.status, PluginStatus.ENABLED)
                updated = uow.plugin_versions.update(replace(target, status=PluginStatus.ENABLED))
                self._write_active_pointer(
                    pointer_path,
                    PluginRef(
                        plugin_id=plugin_id,
                        version=version,
                        archive_sha256=updated.archive_sha256,
                    ).model_dump_json(),
                )
                pointer_changed = True
                return updated
        except Exception:
            if pointer_changed:
                self._restore_active_pointer(pointer_path, previous_pointer)
            raise

    def remove(
        self,
        plugin_id: PluginId,
        version: SemVer,
        *,
        has_active_references: bool = False,
    ) -> PluginVersionRecord:
        if has_active_references:
            raise ValueError("插件版本仍有活动引用，只能停用")
        return self._transition(plugin_id, version, PluginStatus.REMOVED)

    def _transition(self, plugin_id: PluginId, version: SemVer, target: PluginStatus) -> PluginVersionRecord:
        with self._uow_factory() as uow:
            record = self._require(uow, plugin_id, version)
            assert_transition(record.status, target)
            return uow.plugin_versions.update(replace(record, status=target))

    @staticmethod
    def _require(uow: UnitOfWork, plugin_id: PluginId, version: SemVer) -> PluginVersionRecord:
        record = uow.plugin_versions.get(plugin_id, version)
        if record is None:
            raise KeyError(f"插件版本不存在: {plugin_id.root}@{version.root}")
        return record

    def _archive_path(self, plugin_id: PluginId, version: SemVer, digest: Sha256) -> Path:
        del digest
        return self._archive_root / "versions" / plugin_id.root / version.root / "archive.zip"

    def _active_path(self, plugin_id: PluginId) -> Path:
        return self._archive_root / "active" / plugin_id.root / "active.json"

    def _active_record(self, uow: UnitOfWork, plugin_id: PluginId) -> PluginVersionRecord | None:
        path = self._active_path(plugin_id)
        if not path.is_file():
            return None
        try:
            reference = PluginRef.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError("active pointer 不是有效的  PluginRef") from exc
        record = uow.plugin_versions.get(reference.plugin_id, reference.version)
        if record is None or record.plugin_id != plugin_id or record.archive_sha256 != reference.archive_sha256:
            raise ValueError("active pointer 与插件治理记录不一致")
        return record

    @staticmethod
    def _pointer_matches(path: Path, plugin_id: PluginId, version: SemVer) -> bool:
        try:
            reference = PluginRef.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            return False
        return reference.plugin_id == plugin_id and reference.version == version

    @staticmethod
    def _write_active_pointer(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _restore_active_pointer(path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
            return
        PluginGovernanceService._write_active_pointer(
            path,
            content.decode("utf-8"),
        )

    @staticmethod
    def _write_immutable_archive(path: Path, content: bytes) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(content).hexdigest():
                raise ValueError("不可变插件归档摘要冲突")
            return False
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return True
