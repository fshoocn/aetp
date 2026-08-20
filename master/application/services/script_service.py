"""脚本上传/解析服务（P7.3，§18.3）。

职责（§18.3 步骤 1~3）：
1. 校验上传规格（插件 ``upload_spec``：扩展名白名单/大小上限）；
2. 调用同一插件包的 Master 面 ``verify_script`` 快速失败（格式/语法/工程结构错误
   → ``parse_status=failed``，不进入后续流程）；
3. 调用 ``parse_cases`` 生成用例索引，同事务写 ``test_scripts`` + ``script_cases``；
4. 同 hash 重复上传幂等复用既有脚本（sha256 幂等，§6.2）。

解析权威始终在 Master（D-17）；需要台架环境的辅助预检由 Agent 面受控完成，
结果仍回到 Master 统一判定（本服务只处理 Master 面解析）。
"""

from __future__ import annotations

import hashlib
import io
import logging
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Callable

from aetp_protocol.plugin import CaseInfo

from master.application.errors import ScriptNotFoundError
from master.application.services.script_storage_service import ScriptStorageService
from master.domain.enums import (
    ScriptParseLocation,
    ScriptParseStatus,
)
from master.domain.models import ScriptCase, TestScript
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow
from master.plugins.registry import PluginRegistry

logger = logging.getLogger(__name__)


class ScriptUploadError(ValueError):
    """脚本上传/验证失败（错误消息直接面向用户）。"""


class ScriptDeleteError(ValueError):
    """脚本删除失败（通常是仍被任务定义引用）。"""


class ScriptService:
    """脚本上传、验证、解析与用例索引管理。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        plugin_registry: PluginRegistry,
        storage: ScriptStorageService,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = plugin_registry
        self._storage = storage

    # -- 上传 -----------------------------------------------------------------

    async def upload_script(
        self,
        *,
        project_id: str,
        task_type: str,
        name: str,
        config: dict,
        file_data: bytes,
        filename: str,
        created_by: int,
    ) -> TestScript:
        """上传脚本包并同步验证 + 解析（§18.3 步骤 1~3）。

        返回持久化后的 ``TestScript``；同 sha256 重复上传幂等复用既有记录。
        """
        package = self._registry.require(task_type)
        master = package.master

        # 1. 上传规格校验（插件 upload_spec：扩展名/大小）
        self._validate_upload_spec(master.upload_spec, filename, file_data)

        # 2. 内容哈希：同 hash 幂等复用（§6.2/§18.3 步骤 6）
        sha256 = hashlib.sha256(file_data).hexdigest()
        with self._uow_factory() as uow:
            existing = uow.test_scripts.get_by_hash(sha256)
            if existing is not None:
                logger.info(
                    "脚本同 hash 复用: sha256=%s… existing=%s", sha256[:12], existing.script_id
                )
                return existing

        # 3. 解包到临时目录（zip 解压；.py 直接放置），供 verify/parse 使用
        with tempfile.TemporaryDirectory(prefix="aetp-script-") as tmp:
            tmp_dir = Path(tmp)
            self._unpack(file_data, filename, tmp_dir)

            # 4. Master 面 verify_script 快速失败（§18.3 步骤 2）
            errors = master.verify_script(str(tmp_dir), config)
            if errors:
                raise ScriptUploadError("；".join(errors))

            # 5. Master 面 parse_cases 生成用例索引（§18.3 步骤 3）
            cases = await self._parse_cases(master, tmp_dir, config)
            if not cases:
                raise ScriptUploadError("脚本未解析出任何用例")

        # 6. 持久化：同事务写脚本 + 用例（sha256 幂等 + (project,name,version) 唯一）
        script_id = f"S-{uuid.uuid4().hex.upper()}"
        with self._uow_factory() as uow:
            version = self._next_version(uow, project_id, name)
            script = uow.test_scripts.add(
                TestScript(
                    project_id=project_id,
                    script_id=script_id,
                    task_type=task_type,
                    name=name,
                    version=version,
                    file_ref="",
                    size=len(file_data),
                    sha256=sha256,
                    config=config,
                    hardware_requirements=package.master.hardware_requirements(
                        config, cases
                    ),
                    parse_status=ScriptParseStatus.PARSED,
                    parse_location=ScriptParseLocation.MASTER,
                    result_parse_location=ScriptParseLocation.MASTER,
                    plugin_version=package.metadata.plugin_version,
                    created_by=created_by,
                    last_parsed_at=utcnow(),
                )
            )
            uow.script_cases.add_many(
                [
                    ScriptCase(
                        script_id=script_id,
                        case_id=f"C-{uuid.uuid4().hex.upper()}",
                        stable_key=case.stable_key,
                        name=case.name,
                        parent_path=case.parent_path,
                        tags=list(case.tags),
                        params=dict(case.params),
                        avg_duration_s=case.estimated_duration_s,
                        order_index=index,
                    )
                    for index, case in enumerate(cases)
                ]
            )
            # 文件写入存储（DB 只存引用，§6.2）；写入失败回滚事务
            script.file_ref = self._storage.store_script(
                script_id, version, filename, file_data
            )
            uow.test_scripts.update(script)

        logger.info(
            "脚本上传成功: script_id=%s name=%s v%d cases=%d",
            script_id,
            name,
            version,
            len(cases),
        )
        return script

    async def reparse_script(
        self, script_id: str, *, project_id: str
    ) -> TestScript:
        """对已有脚本重新执行验证 + 解析（§18.3 步骤 6，插件升级后显式触发）。"""
        with self._uow_factory() as uow:
            script = uow.test_scripts.get_by_script_id(script_id)
            if script is None or script.project_id != project_id:
                raise ScriptNotFoundError(f"脚本不存在或不属于当前项目: {script_id}")
            data = self._storage.open_script(script.file_ref)
            try:
                file_data = data.read()
            finally:
                data.close()

        package = self._registry.require(script.task_type)
        master = package.master
        with tempfile.TemporaryDirectory(prefix="aetp-script-") as tmp:
            tmp_dir = Path(tmp)
            filename = script.file_ref.rsplit("/", 1)[-1]
            self._unpack(file_data, filename, tmp_dir)
            errors = master.verify_script(str(tmp_dir), script.config)
            if errors:
                raise ScriptUploadError("；".join(errors))
            cases = await self._parse_cases(master, tmp_dir, script.config)
            if not cases:
                raise ScriptUploadError("脚本未解析出任何用例")

        # 重解析：替换用例索引（软删除旧用例，写入新用例；保持 stable_key 稳定）
        with self._uow_factory() as uow:
            script = uow.test_scripts.get_by_script_id(script_id)
            if script is None:
                raise ScriptNotFoundError(f"脚本不存在: {script_id}")
            old_cases = uow.script_cases.list_by_script(
                script_id, include_deleted=True
            )
            old_by_key = {c.stable_key: c for c in old_cases}
            new_keys = {c.stable_key for c in cases}
            for case in old_cases:
                if case.stable_key not in new_keys and not case.deleted:
                    case.deleted = True
                    uow.script_cases.update(case)
            to_add = []
            for index, case in enumerate(cases):
                existing = old_by_key.get(case.stable_key)
                if existing is not None and not existing.deleted:
                    existing.name = case.name
                    existing.parent_path = case.parent_path
                    existing.tags = list(case.tags)
                    existing.params = dict(case.params)
                    existing.order_index = index
                    uow.script_cases.update(existing)
                elif existing is not None:
                    existing.deleted = False
                    existing.name = case.name
                    existing.parent_path = case.parent_path
                    existing.tags = list(case.tags)
                    existing.params = dict(case.params)
                    existing.order_index = index
                    uow.script_cases.update(existing)
                else:
                    to_add.append(
                        ScriptCase(
                            script_id=script_id,
                            case_id=f"C-{uuid.uuid4().hex.upper()}",
                            stable_key=case.stable_key,
                            name=case.name,
                            parent_path=case.parent_path,
                            tags=list(case.tags),
                            params=dict(case.params),
                            avg_duration_s=case.estimated_duration_s,
                            order_index=index,
                        )
                    )
            if to_add:
                uow.script_cases.add_many(to_add)
            script.parse_status = ScriptParseStatus.PARSED
            script.last_parsed_at = utcnow()
            script.plugin_version = package.metadata.plugin_version
            uow.test_scripts.update(script)

        logger.info("脚本重解析完成: script_id=%s cases=%d", script_id, len(cases))
        return script

    def delete_script(self, script_id: str, *, project_id: str) -> None:
        """删除项目脚本及其用例；被启用中的任务定义引用时拒绝删除。

        已停用任务在删除前自动级联清理（硬删除无 Run 的，有 Run 的置空 script_pk）。
        """
        with self._uow_factory() as uow:
            script = uow.test_scripts.get_by_script_id(script_id)
            if script is None or script.project_id != project_id:
                raise ScriptNotFoundError(f"脚本不存在或不属于当前项目: {script_id}")
            references = uow.test_tasks.count_by_script(script_id)
            if references:
                raise ScriptDeleteError(
                    f"脚本仍被 {references} 个启用中的任务定义引用，请先停用或删除任务定义"
                )
            # 级联清理已停用任务：无 Run 的硬删除，有 Run 的置空 script_pk
            cleaned = uow.test_tasks.cleanup_disabled_for_script(script_id)
            if cleaned["deleted"] or cleaned["nullified"]:
                logger.info(
                    "脚本删除级联清理: script_id=%s deleted=%d nullified=%d",
                    script_id,
                    cleaned["deleted"],
                    cleaned["nullified"],
                )
            file_ref = script.file_ref
            uow.script_cases.delete_by_script(script_id)
            uow.test_scripts.delete(script_id)

        self._storage.delete_script(file_ref)
        logger.info("脚本删除成功: script_id=%s project_id=%s", script_id, project_id)

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _validate_upload_spec(spec: dict, filename: str, data: bytes) -> None:
        """按插件 upload_spec 校验扩展名与大小（§18.3 步骤 1）。"""
        suffix = Path(filename).suffix.lower()
        extensions = [str(e).lower() for e in spec.get("extensions", [])]
        if extensions and suffix not in extensions:
            raise ScriptUploadError(
                f"不支持的文件类型 {suffix or '(无扩展名)'}；"
                f"该任务类型仅接受: {', '.join(extensions)}"
            )
        max_mb = int(spec.get("max_size_mb", 100))
        if len(data) > max_mb * 1024 * 1024:
            raise ScriptUploadError(
                f"文件超过大小上限 {max_mb}MB"
            )

    @staticmethod
    def _unpack(data: bytes, filename: str, target: Path) -> None:
        """把上传内容解包到临时目录（zip 解压；单文件直接放置）。"""
        if filename.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                # 防 zip 路径穿越：仅解压安全路径下的成员
                for member in zf.infolist():
                    name = member.filename.replace("\\", "/")
                    if name.startswith("/") or ".." in name.split("/"):
                        continue
                    out = (target / name).resolve()
                    if not str(out).startswith(str(target.resolve())):
                        continue
                    if member.is_dir():
                        out.mkdir(parents=True, exist_ok=True)
                        continue
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(zf.read(member))
        else:
            target.mkdir(parents=True, exist_ok=True)
            (target / filename).write_bytes(data)

    @staticmethod
    async def _parse_cases(master, script_dir: str | Path, config: dict) -> list[CaseInfo]:
        """调用插件 Master 面解析用例（async 契约，直接 await）。"""
        try:
            return list(await master.parse_cases(script_dir, config))
        except Exception as exc:  # noqa: BLE001 - 解析失败统一转为上传错误
            logger.warning("脚本用例解析失败: %s", exc)
            raise ScriptUploadError(f"用例解析失败: {exc}") from exc

    @staticmethod
    def _next_version(uow: UnitOfWork, project_id: str, name: str) -> int:
        """计算同名脚本的下一个版本号（(project, name, version) 唯一）。"""
        version = 1
        while True:
            if (
                uow.test_scripts.find_by_name_version(project_id, name, version)
                is None
            ):
                return version
            version += 1
