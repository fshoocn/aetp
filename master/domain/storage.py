"""对象存储端口（P4.7 存储抽象）。

Master 侧所有文件（测试脚本包、运行产物）的读写统一走本端口，
业务层与 API 层**不得直接使用 pathlib/os 读写文件**：

- 当前实现为本地文件夹（``adapters/storage/local_storage.py``）；
- 后续切换 OSS/S3/MinIO 时只新增一个 adapter，不改业务层。

``key`` 是存储键（相对存储根的路径语义，由实现解释），与数据库
``file_ref`` 字段一一对应：数据库只存引用与 hash（§6.2），不感知底层
是本地目录还是对象存储。
"""

from __future__ import annotations

from typing import BinaryIO, Protocol


class Storage(Protocol):
    """文件/对象的统一读写端口（鸭子类型）。"""

    def put(self, key: str, data: bytes) -> None:
        """写入对象；父级键不存在时自动创建。"""
        ...

    def open(self, key: str) -> BinaryIO:
        """以二进制只读方式打开对象，返回可读流（调用方负责关闭）。"""
        ...

    def exists(self, key: str) -> bool:
        """判断对象是否存在。"""
        ...

    def delete(self, key: str) -> None:
        """删除对象；不存在时静默忽略。"""
        ...

    def list_keys(self, prefix: str = "") -> list[str]:
        """列出存储中给定前缀下的所有对象键（相对键，'/' 分隔）。

        用于孤儿文件扫描：对比数据库中引用的 file_ref 集合，删除无引用的对象。
        """
        ...
