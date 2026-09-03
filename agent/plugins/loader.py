"""Agent 插件多文件 side 目录加载器。

现状：插件入口经 ``importlib.util.spec_from_file_location`` 以单文件方式加载，
入口只能 import 已安装的第三方包（如 ``aetp_protocol``）与标准库，无法 import
同目录的兄弟模块。多文件插件（例如 resource 插件拆成 ``base.py`` + 多个
provider + ``entry.py``）因此无法直接加载。

本模块把插件的某个 side 目录（如 ``agent/``）加载为一个**合成包**（synthetic
package）：为目录创建带 ``__path__`` 的包模块，注册进 ``sys.modules``，再把兄弟
文件作为该包的子模块加载。这样包内文件可以正常使用相对导入（``from .base import
...``）或兄弟导入（``import serial``），加载完清理 ``sys.modules`` 中注册的条目，
不污染进程全局。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TypeVar

T = TypeVar("T")

# 让单个 .py 文件作为包内子模块加载的 spec 工厂
def _load_sibling(package_name: str, directory: Path, filename: str) -> ModuleType:
    module_name = f"{package_name}.{Path(filename).stem}"
    path = directory / filename
    if not path.is_file():
        raise ImportError(f"插件模块不存在: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载插件模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def load_entrypoint(
    side_dir: str | Path,
    entry_ref: str,
    *,
    package_prefix: str = "aetp_plugin",
    factory_attr: str | None = None,
) -> tuple[ModuleType, Callable[[], object]]:
    """把 side 目录作为合成包加载入口，返回 (入口模块, 工厂可调用对象)。

    ``entry_ref`` 形如 ``entry:create_providers``（模块:属性）。兄弟模块用相对导入
    访问（要求入口所在目录被当作包）。``side_dir`` 是插件 side 目录（如 agent/）。
    """
    root = Path(side_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"插件 side 目录不存在: {root}")

    module_part, attribute_part = entry_ref.split(":", 1)
    entry_file = module_part.replace(".", "/").rsplit("/", 1)[-1] + ".py"
    package_name = f"{package_prefix}.{_safe_key(root)}"

    # 创建合成包：目录即包，__path__ 指向 side 目录
    package = ModuleType(package_name)
    package.__package__ = package_name
    package.__path__ = [str(root)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    registered: list[str] = [package_name]
    try:
        entry = _load_sibling(package_name, root, entry_file)
        registered.append(entry.__name__)
        factory = getattr(entry, attribute_part, None)
        if not callable(factory):
            raise TypeError(f"插件入口不可调用: {entry_ref}")
        if factory_attr is not None:
            return entry, factory
        return entry, factory
    finally:
        # 卸载已注册模块；注意仅移除本包及其子模块，不影响其它
        for name in reversed(registered):
            sys.modules.pop(name, None)
        for name in list(sys.modules):
            if name.startswith(package_name + "."):
                sys.modules.pop(name, None)


def _safe_key(path: Path) -> str:
    # 用目录名（含上级父目录名）生成唯一、安全的模块片段
    parent = path.parent.name or "root"
    child = path.name
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in f"{parent}_{child}")
    return cleaned or "plugin"


__all__ = ["load_entrypoint"]
