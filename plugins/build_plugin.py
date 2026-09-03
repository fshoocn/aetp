"""通用 AETP V2 插件归档构建脚本。

用法（仓库根）：
    python plugins/build_plugin.py plugins/pytest_plugin
    python plugins/build_plugin.py plugins/junit_reporter
    python plugins/build_plugin.py plugins/case_statistics_analyzer
    python plugins/build_plugin.py plugins/resource_package          # 多资源 provider 包（后述）

读取目标目录的 plugin.json，按 point 决定归档内容，产出
``plugins/{plugin_id}-{version}.zip``（archive 目录为仓库根 plugins/）。

归档内容规则：
- 总是包含根 ``plugin.json``。
- ``master/``、``agent/`` 目录存在时整目录递归加入（跳过 __pycache__、.pyc、tests）。
- 除 plugin.json 外顶层文件（如 README 之外）只加白名单（见 _TOP_LEVEL_FILES）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import zipfile

# 顶层允许随包携带的文件（不含 master/agent 目录内容）
_TOP_LEVEL_FILES = {
    "plugin.json",
    "README.md",
    "LICENSE",
    "NOTICE",
    "CHANGELOG.md",
}


def _iter_members(plugin_root: pathlib.Path, point: str) -> list[str]:
    """按 point/目录存在性决定 zip 成员（zip 内相对路径）。"""
    members: list[str] = []
    manifest = plugin_root / "plugin.json"
    if not manifest.is_file():
        raise SystemExit(f"缺少 plugin.json: {plugin_root}")

    members.append("plugin.json")
    for side in ("master", "agent", "ui", "schemas"):
        side_dir = plugin_root / side
        if side_dir.is_dir():
            for path in sorted(side_dir.rglob("*")):
                if not path.is_file():
                    continue
                parts = path.relative_to(plugin_root).parts
                if "__pycache__" in parts or path.suffix in {".pyc", ".pyo"}:
                    continue
                if "tests" in parts:
                    continue
                members.append(path.relative_to(plugin_root).as_posix())
    for name in sorted(plugin_root.iterdir()):
        if name.is_file() and name.name in _TOP_LEVEL_FILES and name.name != "plugin.json":
            members.append(name.name)
    return members


def build(plugin_root: pathlib.Path, output_dir: pathlib.Path) -> pathlib.Path:
    plugin_root = plugin_root.resolve()
    manifest = json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2:
        raise SystemExit(f"plugin.json schema_version 必须为 2: {plugin_root}")
    plugin_id = manifest["id"]
    version = manifest["version"]
    members = _iter_members(plugin_root, manifest.get("point", ""))
    missing = [name for name in members if not (plugin_root / name).exists()]
    if missing:
        raise SystemExit(f"缺少归档条目: {', '.join(missing)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f"{plugin_id}-{version}.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            archive.write(plugin_root / member, member)
        print("归档内容:")
        for name in sorted(archive.namelist()):
            print("  " + name)
    print(f"OK -> {out} ({out.stat().st_size} bytes)")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 AETP V2 插件归档")
    parser.add_argument("plugin_dir", help="插件工程目录（含 plugin.json）")
    args = parser.parse_args()
    plugin_root = pathlib.Path(args.plugin_dir)
    if not plugin_root.is_dir() or not (plugin_root / "plugin.json").is_file():
        print(f"无效插件目录: {plugin_root}", file=sys.stderr)
        return 2
    repo_root = pathlib.Path(__file__).resolve().parent
    build(plugin_root, repo_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
