"""结束产物文件存储服务（P6.6，§6.2/§18.8）。

统一产物的存储键规则与读写，与 ``ScriptStorageService`` 同构：
产物存 ``artifacts/{run_id}/{filename}``，DB 只存引用与 hash。
读写统一经 ``Storage`` 端口，不直接操作文件系统。
"""

from __future__ import annotations

from typing import BinaryIO

from master.domain.storage import Storage


class ArtifactStorageService:
    """运行产物的文件存储编排。"""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    @staticmethod
    def artifact_key(run_id: str, filename: str) -> str:
        """生成产物存储键（上传时写入 file_ref）。"""
        return f"artifacts/{run_id}/{filename}"

    def store(self, run_id: str, filename: str, data: bytes) -> str:
        """写入产物文件并返回存储键（file_ref）。"""
        key = self.artifact_key(run_id, filename)
        self._storage.put(key, data)
        return key

    def open(self, file_ref: str) -> BinaryIO:
        """打开产物文件读取流（调用方负责关闭）。"""
        return self._storage.open(file_ref)

    def exists(self, file_ref: str) -> bool:
        """判断产物文件是否存在。"""
        return self._storage.exists(file_ref)
