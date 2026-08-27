"""存储孤儿文件清理服务（§6.2 补充）。

脚本（``scripts/``）与产物（``artifacts/``）写入文件后，数据库引用登记若在
进程崩溃时未完成，会遗留无引用的孤儿文件。本服务周期性对比存储中的对象键
与数据库 file_ref 集合，删除无引用的对象（best-effort，不触碰数据库事务）。

只清理 ``scripts/`` 与 ``artifacts/`` 两个已知前缀，不误删插件包等其他数据。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from master.domain.repositories import UnitOfWork
from master.domain.storage import Storage

logger = logging.getLogger(__name__)

# 参与孤儿扫描的存储前缀（与 file_ref 路径规则一致）
_SCRIPT_PREFIX = "scripts"
_ARTIFACT_PREFIX = "artifacts"


class StorageCleanupService:
    """孤儿文件扫描与清理。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        storage: Storage,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage

    def cleanup_orphans(self) -> dict[str, int]:
        """扫描并删除无数据库引用的存储对象，返回清理统计。

        不删除任何仍被引用的对象；单文件删除失败只记录日志并继续。
        """
        with self._uow_factory() as uow:
            script_refs = set(uow.test_scripts.list_all_file_refs())
            artifact_refs = set(uow.run_artifacts.list_all_file_refs())

        referenced = script_refs | artifact_refs

        removed = 0
        errors = 0
        for prefix in (_SCRIPT_PREFIX, _ARTIFACT_PREFIX):
            for key in self._storage.list_keys(prefix):
                if key in referenced:
                    continue
                try:
                    self._storage.delete(key)
                    removed += 1
                except Exception:
                    logger.exception("孤儿文件删除失败: key=%s", key)
                    errors += 1

        if removed or errors:
            logger.info("孤儿文件清理完成: removed=%d errors=%d", removed, errors)
        return {"removed": removed, "errors": errors}
