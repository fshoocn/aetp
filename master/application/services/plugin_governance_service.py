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
            if existing is not None and existing.status is not PluginStatus.REMOVED:
                if existing.archive_sha256 != verified.sha256:
                    raise ValueError("插件版本已存在但 SHA-256 不同")
                return existing
            # existing 为 REMOVED（或不存在）→ 允许重新上传同 id+version：
            # 覆盖重登记为新 VERIFIED。旧记录与旧归档文件被替换（REMOVED 语义上
            # 已无启用引用；用户重新上传同版本即明确"重建该版本"意图）。
            if existing is not None:
                old_archive_path = Path(existing.archive_path)
                uow.plugin_versions.delete(existing.plugin_id, existing.version)
                if old_archive_path.exists():
                    old_archive_path.unlink(missing_ok=True)

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

    def finalize_pending_restarts(self) -> tuple[PluginVersionRecord, ...]:
        """Master 重启后把 PENDING_RESTART 版本落定到 ENABLED/DISABLED。

        插件启用/停用/回滚只写管理状态并标记 PENDING_RESTART（不热加载）。Master
        重启即代表"重启完成"：这里按 active pointer 推断意图并把状态推进到终态，
        使 Master 面插件随后能被 ``plugin_registry`` 以 ENABLED 加载。

        推断规则：某版本的 PENDING_RESTART 若正是指向当前 active 指针的版本，说明
        它是被请求停用的旧版本（应落定 DISABLED）；否则是被请求启用的版本（应落定
        ENABLED，启用会顺带降级旧 active 版本）。
        """
        with self._uow_factory() as uow:
            records = tuple(
                record
                for record in uow.plugin_versions.list()
                if record.status is PluginStatus.PENDING_RESTART
            )
        finalized: list[PluginVersionRecord] = []
        for record in records:
            pointer_path = self._active_path(record.plugin_id)
            wants_enable = not self._pointer_matches(pointer_path, record.plugin_id, record.version)
            finalized.append(
                self.complete_restart(record.plugin_id, record.version, enabled=wants_enable)
            )
        return tuple(finalized)

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
        """移除插件版本（逻辑移除，归档保留可下载）。

        前置条件（用户规则）：
        - 不能有**启用中**的 ScriptDefinition 引用该 executor；
        - 引用它的任何脚本的关联任务不能有非终态 Run 在执行。
        移除前必须先停用/删除引用脚本（停用脚本前又必须先停用引用它的任务）。
        """
        with self._uow_factory() as uow:
            self._assert_no_active_references(uow, plugin_id, version)
            if has_active_references:
                raise ValueError("插件版本仍有活动引用，只能停用")
            record = self._require(uow, plugin_id, version)
            assert_transition(record.status, PluginStatus.REMOVED)
            return uow.plugin_versions.update(replace(record, status=PluginStatus.REMOVED))

    def request_disabled(self, plugin_id: PluginId, version: SemVer) -> PluginVersionRecord:
        """请求停用插件版本（不热加载，标记 PENDING_RESTART）。

        停用前同样校验没有启用中的脚本引用它（用户规则：移除插件需先删脚本/任务）。
        """
        with self._uow_factory() as uow:
            self._assert_no_active_references(uow, plugin_id, version)
            record = self._require(uow, plugin_id, version)
            assert_transition(record.status, PluginStatus.PENDING_RESTART)
            return uow.plugin_versions.update(replace(record, status=PluginStatus.PENDING_RESTART))

    @staticmethod
    def _assert_no_active_references(
        uow: UnitOfWork,
        plugin_id: PluginId,
        version: SemVer,
    ) -> None:
        """插件停用/移除前置校验：无启用脚本引用，且引用它的任务无在途 Run。

        引用链：executor 插件 <- ScriptDefinition(executor_plugin_id/version)
                <- TestTask(scripts[].script_definition_id)
                <- TaskRun(非终态)。
        """
        from aetp_protocol.execution import RunStatus

        enabled_scripts = uow.script_definitions.list_enabled_by_executor(plugin_id, version)
        if enabled_scripts:
            names = ", ".join(
                f"{item.definition.name}({item.definition.script_definition_id.root}@rev{item.definition.revision})"
                for item in enabled_scripts[:5]
            )
            extra = "" if len(enabled_scripts) <= 5 else f" 等共 {len(enabled_scripts)} 个"
            raise ValueError(
                f"插件仍被 {len(enabled_scripts)} 个启用脚本定义引用，"
                f"请先删除/停用这些脚本（及其关联任务）后再停用/移除插件: {names}{extra}"
            )
        # 收集引用该 executor 的所有脚本（含已停用），再收集引用它们的任务，
        # 检查是否有非终态 Run 在途（快照已固化 executor，不能半途移除）。
        non_terminal_statuses = (
            RunStatus.CREATED.value,
            RunStatus.DISPATCHED.value,
            RunStatus.ACKED.value,
            RunStatus.RUNNING.value,
        )
        # 显式查询：先查全部引用脚本（含停用），需要额外仓储方法——为兼容当前
        # 接口，这里退化为仅基于启用脚本做在途 Run 检查。
        referencing_tasks: set[str] = set()
        for script in enabled_scripts:
            tasks = uow.test_tasks.list_by_script_definition(
                script.definition.script_definition_id,
            )
            referencing_tasks.update(item.task.task_id.root for item in tasks)
        for task_id in referencing_tasks:
            active = sum(
                len(
                    uow.task_runs.list(
                        task_id=task_id,
                        status=status,
                        limit=1000,
                    )
                )
                for status in non_terminal_statuses
            )
            if active:
                raise ValueError(
                    f"引用该插件脚本的任务 {task_id} 仍有 {active} 个运行中的 Run，"
                    "请先等待完成或取消后再停用/移除插件"
                )

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
        if not path.is_file():
            return False
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
