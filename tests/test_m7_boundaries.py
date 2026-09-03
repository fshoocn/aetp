"""M7-4： 生产模块不得重新依赖 legacy 执行契约。"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).parents[1]
__MODULE_ROOTS = (
    _REPO_ROOT / "master" / "api" / "v2",
    _REPO_ROOT / "master" / "plugins" / "registry.py",
    _REPO_ROOT / "master" / "plugins" / "extension_resolver.py",
    _REPO_ROOT / "master" / "application" / "services" / "*.py",
    _REPO_ROOT / "agent" / "plugins" / "registry.py",
    _REPO_ROOT / "agent" / "plugins" / "installer.py",
    _REPO_ROOT / "agent" / "application" / "services" / "*.py",
)
_FORBIDDEN_LEGACY_SYMBOLS = ("task_type", "run.assign", "ui_protocol")


def _production_files() -> tuple[Path, ...]:
    files: set[Path] = set()
    for root in __MODULE_ROOTS:
        if root.is_dir():
            files.update(path for path in root.rglob("*.py") if path.is_file())
        elif root.is_file():
            files.add(root)
        else:
            files.update(path for path in root.parent.glob(root.name) if path.is_file())
    return tuple(sorted(files))


def test_production_modules_do_not_reference_legacy_contracts() -> None:
    violations: list[str] = []
    for path in _production_files():
        content = path.read_text(encoding="utf-8")
        for symbol in _FORBIDDEN_LEGACY_SYMBOLS:
            if symbol in content:
                violations.append(f"{path.relative_to(_REPO_ROOT)}: {symbol}")
    assert violations == [], " 生产模块重新引入 legacy 契约:\n" + "\n".join(violations)
