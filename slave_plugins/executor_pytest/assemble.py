"""工程组装（DESIGN §10.2）：材料 zip → 测试工程目录的配方解释。

* **解压**：zip-slip 防护（绝对路径 / ``..`` 穿越 / 盘符前缀一律拒绝），
  落盘后逐条 ``resolve().is_relative_to(dest)`` 复核；
* **铺排**：按 ``recipe.layout`` 的 ``src → dst`` 铺排（复制后移除源），
  ``src`` / ``dst`` 解析后必须仍在工程目录内；
* **配方覆盖**（§4.2）：``recipe_override`` **浅合并**——标量 / 对象同名替换、
  **数组整体替换**（含 ``layout``）；顶层键整体换值，不做元素级合并；
* 失败抛 :class:`AssembleError`，上层折算 ``failed: assemble error``（不重试）。

``setup`` 命令 v1 **不执行只记录**（决策 D15，防上传内容借配方执行任意命令）。
"""

from __future__ import annotations

import shutil
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, cast


class AssembleError(Exception):
    """组装失败（zip 损坏 / 穿越 / 配方不合法），上层不重试。"""


def merge_recipe(base: Mapping[str, Any], override: Mapping[str, Any] | None) -> dict[str, Any]:
    """配方浅合并（§4.2）：顶层键同名替换、数组整体替换；返回新字典。"""
    merged: dict[str, Any] = {str(key): value for key, value in base.items()}
    if override:
        for key, value in override.items():
            merged[str(key)] = value
    return merged


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


def apply_layout(root: Path, recipe: Mapping[str, Any]) -> None:
    """按 ``recipe.layout`` 在工程目录内铺排 ``src → dst``（复制后移除源）。"""
    layout_raw = recipe.get("layout")
    if layout_raw is None:
        return
    if not isinstance(layout_raw, list):
        raise AssembleError(f"recipe.layout 需是数组，收到 {type(layout_raw).__name__}")
    base = root.resolve()
    for index, entry in enumerate(cast(list[object], layout_raw)):
        if not isinstance(entry, Mapping):
            raise AssembleError(f"recipe.layout[{index}] 需是对象")
        item = cast(Mapping[object, object], entry)
        src_rel, dst_rel = item.get("src"), item.get("dst")
        if not isinstance(src_rel, str) or not src_rel.strip():
            raise AssembleError(f"recipe.layout[{index}].src 需是非空字符串")
        if not isinstance(dst_rel, str) or not dst_rel.strip():
            raise AssembleError(f"recipe.layout[{index}].dst 需是非空字符串")
        src = _inside(base, src_rel, f"recipe.layout[{index}].src")
        dst = _inside(base, dst_rel, f"recipe.layout[{index}].dst")
        if not src.exists():
            raise AssembleError(f"recipe.layout[{index}] 源不存在：{src_rel}")
        _move_entry(src, dst)


def _inside(base: Path, relative: str, label: str) -> Path:
    """把相对路径解析到 ``base`` 内；越界即组装失败。"""
    rel = PurePosixPath(relative.replace("\\", "/"))
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise AssembleError(f"{label} 路径非法：{relative}")
    if rel.parts and ":" in rel.parts[0]:
        raise AssembleError(f"{label} 路径非法：{relative}")
    target = (base / Path(*rel.parts)).resolve()
    if not target.is_relative_to(base):
        raise AssembleError(f"{label} 越出工程目录：{relative}")
    return target


def _move_entry(src: Path, dst: Path) -> None:
    """铺排一步：目录合并复制后删源，文件覆盖复制后删源。"""
    if src == dst:
        return
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        shutil.rmtree(src)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    src.unlink()


def validate_recipe(recipe: Mapping[str, Any]) -> list[str]:
    """配方结构校验（inspect 的 ``recipe.invalid`` 用）；返回问题消息列表。

    只查结构：``layout`` 条目形制、``entry`` / ``args`` / ``env`` / ``requirements``
    类型与 ``env`` 的 ``AETP_*`` 保留前缀（§15 纵深）。``setup`` 仅记录不执行
    （D15），不参与校验。
    """
    problems: list[str] = []
    layout_raw = recipe.get("layout")
    if layout_raw is not None:
        if not isinstance(layout_raw, list):
            problems.append(f"recipe.layout 需是数组，收到 {type(layout_raw).__name__}")
        else:
            for index, entry in enumerate(cast(list[object], layout_raw)):
                if not isinstance(entry, Mapping):
                    problems.append(f"recipe.layout[{index}] 需是对象")
                    continue
                item = cast(Mapping[object, object], entry)
                if not isinstance(item.get("src"), str) or not str(item.get("src")).strip():
                    problems.append(f"recipe.layout[{index}].src 需是非空字符串")
                if not isinstance(item.get("dst"), str) or not str(item.get("dst")).strip():
                    problems.append(f"recipe.layout[{index}].dst 需是非空字符串")
    entry = recipe.get("entry")
    if entry is not None and (not isinstance(entry, str) or not entry.strip()):
        problems.append("recipe.entry 需是非空字符串")
    for name in ("args", "requirements", "setup"):
        value = recipe.get(name)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) for item in cast(list[object], value)):
            problems.append(f"recipe.{name} 需是字符串数组")
    env = recipe.get("env")
    if env is not None:
        if not isinstance(env, Mapping):
            problems.append("recipe.env 需是对象")
        else:
            for key, value in cast(Mapping[object, object], env).items():
                if not isinstance(key, str) or not isinstance(value, str):
                    problems.append("recipe.env 的键值都需是字符串")
                    break
                if key.startswith("AETP_"):
                    problems.append(f"recipe.env 拒绝 AETP_* 保留前缀：{key}")
    return problems


__all__: list[str] = [
    "AssembleError",
    "apply_layout",
    "extract_zip",
    "merge_recipe",
    "validate_recipe",
]
