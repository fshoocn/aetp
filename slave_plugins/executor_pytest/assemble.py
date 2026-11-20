"""工程组装（DESIGN §10.2）：材料 zip → 测试工程目录的安全解压。

* **解压**：zip-slip 防护（绝对路径 / ``..`` 穿越 / 盘符前缀一律拒绝），
  落盘后逐条 ``resolve().is_relative_to(dest)`` 复核；符号链接条目拒绝；
* 不做任何配方解释：recipe 是执行器自定义**不透明字符串**（core 不解析、
  执行端不解包），材料包内容平铺解压即工程目录；
* 失败抛 :class:`AssembleError`，上层折算 ``failed: assemble error``（不重试）。
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path, PurePosixPath


class AssembleError(Exception):
    """组装失败（zip 损坏 / 穿越），上层不重试。"""


def extract_zip(zip_path: Path, dest: Path) -> None:
    """把材料 zip 解压到 ``dest``（zip-slip + ``is_relative_to`` 双校验）。"""
    if not zip_path.is_file():
        raise AssembleError(f"材料包不存在：{zip_path}")
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        with zipfile.ZipFile(zip_path) as package:
            for member in package.infolist():
                _extract_member(package, member, root)
    except zipfile.BadZipFile as exc:
        raise AssembleError(f"材料包不是合法 zip：{exc}") from exc


def _extract_member(package: zipfile.ZipFile, member: zipfile.ZipInfo, root: Path) -> None:
    """解压单个成员并校验落点在 ``root`` 内（拒绝符号链接条目）。"""
    rel = PurePosixPath(member.filename.replace("\\", "/"))
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise AssembleError(f"材料包含非法路径：{member.filename}")
    if rel.parts and ":" in rel.parts[0]:
        raise AssembleError(f"材料包含非法路径：{member.filename}")
    if (member.external_attr >> 16) & 0o170000 == 0o120000:
        raise AssembleError(f"材料包不允许符号链接：{member.filename}")
    target = (root / Path(*rel.parts)).resolve()
    if not target.is_relative_to(root):
        raise AssembleError(f"材料包路径穿越：{member.filename}")
    if member.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with package.open(member) as source, target.open("wb") as output:
        shutil.copyfileobj(source, output)


__all__: list[str] = [
    "AssembleError",
    "extract_zip",
]
