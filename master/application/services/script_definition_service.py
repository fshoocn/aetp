""" ScriptDefinition 上传、解析和不可变登记。"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Mapping
from pathlib import Path

from aetp_protocol.artifacts import Configuration, ScriptRef, TestCase
from aetp_protocol.execution import ExecutionRequirement, PluginRequirement
from aetp_protocol.ids import BusinessId, PluginId, SemVer, Sha256, VersionRange, new_id
from aetp_protocol.plugin_types import PluginPoint, PluginRef, PluginStatus
from aetp_protocol.task import ScriptDefinition

from common.zip_utils import safe_extract_zip
from master.application.services.script_storage_service import ScriptStorageService
from master.domain.models import ScriptDefinitionRecord
from master.domain.time import utcnow
from master.plugins.extension_resolver import ExtensionResolver
from master.plugins.registry import PluginRegistry


class ScriptDefinitionError(ValueError):
    """ 脚本上传或解析失败。"""


class ScriptDefinitionService:
    """用精确  executor 生成 ScriptDefinition。"""

    MAX_SIZE = 100 * 1024 * 1024

    def __init__(
        self,
        uow_factory,
        storage: ScriptStorageService,
        plugin_registry: PluginRegistry,
        executor_resolver: ExtensionResolver,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage
        self._plugin_registry = plugin_registry
        self._executor_resolver = executor_resolver

    async def upload(
        self,
        *,
        project_id: BusinessId,
        name: str,
        executor_plugin_id: PluginId,
        executor_version: SemVer,
        configuration: Configuration,
        filename: str,
        file_data: bytes,
        created_by: int,
    ) -> ScriptDefinitionRecord:
        if not name.strip():
            raise ScriptDefinitionError("ScriptDefinition 名称不能为空")
        if not file_data:
            raise ScriptDefinitionError("脚本文件不能为空")
        if len(file_data) > self.MAX_SIZE:
            raise ScriptDefinitionError("脚本文件不能超过 100 MB")
        try:
            self._storage.validate_filename(filename)
        except ValueError as exc:
            raise ScriptDefinitionError(str(exc)) from exc

        record = self._plugin_registry.get(
            executor_plugin_id,
            executor_version,
            PluginPoint.EXECUTOR,
        )
        if record is None or record.status is not PluginStatus.ENABLED:
            raise ScriptDefinitionError(
                f" executor 未启用: {executor_plugin_id.root}@{executor_version.root}"
            )
        resolved = self._executor_resolver.resolve(
            record,
            PluginPoint.EXECUTOR,
            required_method="parse_cases",
        )
        parser = resolved.plugin
        definition_id = BusinessId(new_id())
        digest = hashlib.sha256(file_data).hexdigest()
        try:
            cases = await self._parse_cases(parser, file_data, filename, configuration.values)
            if not cases:
                raise ScriptDefinitionError("脚本未解析出任何用例")
            source = ScriptRef(
                script_id=definition_id,
                version=1,
                filename=filename,
                size=len(file_data),
                sha256=Sha256(digest),
                download_url=None,
            )
            definition = ScriptDefinition(
                script_definition_id=definition_id,
                project_id=project_id,
                revision=1,
                name=name.strip(),
                executor=PluginRef(
                    plugin_id=executor_plugin_id,
                    version=executor_version,
                    archive_sha256=record.archive_sha256,
                ),
                source=source,
                configuration=configuration,
                cases=tuple(cases),
                requirement=ExecutionRequirement(
                    executor=PluginRequirement(
                        plugin_id=executor_plugin_id,
                        version=VersionRange(exact=executor_version),
                    )
                ),
                enabled=True,
            )
            with self._uow_factory() as uow:
                if uow.projects.get_by_project_id(project_id.root) is None:
                    raise ScriptDefinitionError(f"项目不存在: {project_id.root}")
                self._storage.store_script(
                    definition_id.root,
                    source.version,
                    filename,
                    file_data,
                )
                try:
                    return uow.script_definitions.add(
                        ScriptDefinitionRecord(
                            id=None,
                            definition=definition,
                            created_at=utcnow(),
                            updated_at=utcnow(),
                        )
                    )
                except Exception:
                    self._storage.delete_script(
                        self._storage.script_key(definition_id.root, source.version, filename)
                    )
                    raise
        except ScriptDefinitionError:
            raise
        except Exception as exc:
            raise ScriptDefinitionError(f" 脚本解析失败: {exc}") from exc

    async def _parse_cases(
        self,
        parser,
        file_data: bytes,
        filename: str,
        configuration: Mapping[str, object],
    ) -> list[TestCase]:
        with tempfile.TemporaryDirectory(prefix="aetp-script-") as raw_dir:
            script_dir = Path(raw_dir)
            if filename.lower().endswith(".zip"):
                safe_extract_zip(file_data, script_dir)
            else:
                script_dir.mkdir(parents=True, exist_ok=True)
                (script_dir / "test_script.py").write_bytes(file_data)
            raw_cases = await parser.parse_cases(script_dir, dict(configuration))
        cases: list[TestCase] = []
        for raw in raw_cases:
            if isinstance(raw, Mapping):
                stable_key = raw.get("stable_key")
                case_name = raw.get("name")
                parent_path = raw.get("parent_path", "")
                tags = raw.get("tags", ())
                estimated = raw.get("estimated_duration_s")
            else:
                stable_key = getattr(raw, "stable_key", None)
                case_name = getattr(raw, "name", None)
                parent_path = getattr(raw, "parent_path", "")
                tags = getattr(raw, "tags", ())
                estimated = getattr(raw, "estimated_duration_s", None)
            if not isinstance(stable_key, str) or not stable_key.strip():
                raise ScriptDefinitionError("executor 返回了无效 case stable_key")
            if not isinstance(case_name, str) or not case_name.strip():
                case_name = stable_key.rsplit("::", 1)[-1]
            if not isinstance(parent_path, str):
                parent_path = ""
            if not isinstance(tags, (list, tuple)) or not all(isinstance(tag, str) for tag in tags):
                tags = ()
            if estimated is not None and (
                isinstance(estimated, bool) or not isinstance(estimated, (int, float)) or estimated < 0
            ):
                estimated = None
            cases.append(
                TestCase(
                    stable_key=stable_key,
                    name=case_name,
                    parent_path=parent_path,
                    tags=tuple(tags),
                    estimated_duration_s=estimated,
                )
            )
        return cases


__all__ = ["ScriptDefinitionError", "ScriptDefinitionService"]
