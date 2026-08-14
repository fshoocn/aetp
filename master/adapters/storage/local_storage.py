"""本地文件夹存储实现（Storage 端口）。"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from master.domain.storage import Storage


class LocalStorage:
    """将存储键映射到本地目录下的文件。

    - 相对键：基于 ``root`` 解析（默认 ``data/``）；
    - 绝对路径键：直接使用（兼容历史/测试中的绝对 file_ref）。
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        path = Path(key)
        return path if path.is_absolute() else self._root / path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()
