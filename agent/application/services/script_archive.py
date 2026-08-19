"""Agent 脚本 ZIP 安全解包工具。"""

from __future__ import annotations

import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath


def extract_zip_safely(source: Path, target: Path) -> None:
    """解包 ZIP，拒绝目录穿越、绝对路径和符号链接条目。"""
    target_root = target.resolve()
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            normalized = member.filename.replace("\\", "/")
            relative = PurePosixPath(normalized)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"ZIP 包含不安全路径: {member.filename}")
            mode = (member.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError(f"ZIP 包含符号链接: {member.filename}")
            destination = (target / Path(*relative.parts)).resolve()
            if destination != target_root and target_root not in destination.parents:
                raise ValueError(f"ZIP 路径越界: {member.filename}")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source_file, destination.open("wb") as target_file:
                shutil.copyfileobj(source_file, target_file)
