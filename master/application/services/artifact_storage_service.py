"""结束产物文件存储服务（P6.6，§6.2/§18.8）。

统一产物的存储键规则与读写，与 ``ScriptStorageService`` 同构：
产物存 ``artifacts/{run_id}/{filename}``，DB 只存引用与 hash。
读写统一经 ``Storage`` 端口，不直接操作文件系统。
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from master.domain.storage import Storage


class ArtifactStorageService:
    """运行产物的文件存储编排。"""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    @staticmethod
    def artifact_key(
        run_id: str,
        filename: str,
        *,
        shard_id: str | None = None,
        attempt_id: str | None = None,
    ) -> str:
        """生成产物存储键（上传时写入 file_ref）。"""
        path = Path(filename)
        if not filename or filename in {".", ".."} or path.is_absolute() or path.name != filename:
            raise ValueError("产物文件名必须是存储根目录下的单个文件名")
        if attempt_id is not None and shard_id is None:
            raise ValueError("attempt_id 必须同时提供 shard_id")
        if shard_id is not None:
            prefix = f"artifacts/{run_id}/shards/{shard_id}"
            if attempt_id is not None:
                prefix += f"/attempts/{attempt_id}"
            return f"{prefix}/{filename}"
        return f"artifacts/{run_id}/{filename}"

    def store(
        self,
        run_id: str,
        filename: str,
        data: bytes,
        *,
        shard_id: str | None = None,
        attempt_id: str | None = None,
    ) -> str:
        """写入产物文件并返回存储键（file_ref）。"""
        key = self.artifact_key(run_id, filename, shard_id=shard_id, attempt_id=attempt_id)
        self._storage.put(key, data)
        return key

    def open(self, file_ref: str) -> BinaryIO:
        """打开产物文件读取流（调用方负责关闭）。"""
        return self._storage.open(file_ref)

    def exists(self, file_ref: str) -> bool:
        """判断产物文件是否存在。"""
        return self._storage.exists(file_ref)

    def delete(self, file_ref: str) -> None:
        """删除已写入的产物文件，用于引用登记失败时补偿。"""
        self._storage.delete(file_ref)
