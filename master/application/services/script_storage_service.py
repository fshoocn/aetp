"""脚本文件存储服务（P4.7，存储抽象）。

统一脚本文件的存储键规则与读写入口，上传（P7.3 落地时调用
``store_script``）与下载（内部端点调用 ``open_script``）都只依赖
``Storage`` 端口，不直接操作文件系统；将来切换云存储只替换 adapter。

存储键规则：``scripts/{script_id}/{version}/{filename}``，与数据库
``file_ref`` 一一对应（§6.2：DB 只存引用与 hash）。
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from master.domain.storage import Storage


class ScriptStorageService:
    """脚本包的文件存储编排。"""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    @staticmethod
    def script_key(script_id: str, version: int, filename: str) -> str:
        """生成脚本文件的存储键（上传时写入 file_ref）。"""
        ScriptStorageService.validate_filename(filename)
        return f"scripts/{script_id}/{version}/{filename}"

    @staticmethod
    def validate_filename(filename: str) -> None:
        """只允许单个文件名，防止用户输入改变存储键的目录层级。"""
        if (
            not filename
            or filename in {".", ".."}
            or "\x00" in filename
            or "/" in filename
            or "\\" in filename
            or Path(filename).is_absolute()
        ):
            raise ValueError("脚本文件名不能包含路径")

    def store_script(self, script_id: str, version: int, filename: str, data: bytes) -> str:
        """写入脚本文件并返回存储键（file_ref）。"""
        key = self.script_key(script_id, version, filename)
        self._storage.put(key, data)
        return key

    def open_script(self, file_ref: str) -> BinaryIO:
        """打开脚本文件读取流（调用方负责关闭）。"""
        return self._storage.open(file_ref)

    def script_exists(self, file_ref: str) -> bool:
        """判断脚本文件是否存在。"""
        return self._storage.exists(file_ref)

    def delete_script(self, file_ref: str) -> None:
        """删除脚本文件（引用保护下的物理删除时使用）。"""
        self._storage.delete(file_ref)
