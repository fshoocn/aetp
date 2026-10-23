"""插件专属数据目录约定：``<节点目录>/data/plugins/{plugin_id}/``。

插件运行期自建的**数据文件**（自管 sqlite、缓存、导出物等）放这里，插件
卸载时由插件管理器整目录回收（``keep_data=True`` 保留，与 ``drop_owner``
的表语义一致）。三类存储的生命周期对照：

* 插件包目录（``plugins/{id}``）：代码与静态资源——卸载即删；
* **专属数据目录**（``data/plugins/{id}``，本模块）：插件自管数据文件——卸载即删；
* ``ctx.db`` 的表（``aetp_{owner}_*``）：入库数据——卸载走 ``drop_owner``。

包目录不能放数据（覆盖安装替换目录会连带丢数据），数据文件请用本目录。
与 db / log 的默认数据位置同根（节点目录下 ``data/``）。插件侧用法::

    from common.plugin_data import plugin_data_dir

    path = plugin_data_dir(base_dir, plugin_id)   # 使用前自行 mkdir(parents=True)

其中 ``base_dir`` 是节点目录（``config.ini`` 所在目录）。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 插件 id（与插件管理器 ``PLUGIN_ID`` 同规则）：小写字母数字开头，仅 - 与 _
_PLUGIN_ID: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def plugin_data_root(base_dir: Path) -> Path:
    """全部插件专属数据目录的父目录：``<节点目录>/data/plugins``。"""
    return base_dir / "data" / "plugins"


def plugin_data_dir(base_dir: Path, plugin_id: str) -> Path:
    """单个插件的专属数据目录：``<节点目录>/data/plugins/{plugin_id}``。

    参数:
        base_dir: 节点目录（``config.ini`` 所在目录）。
        plugin_id: 插件 id；非法（含路径穿越）抛 ``ValueError``。

    返回:
        目录路径（不保证已存在，使用前自行 ``mkdir(parents=True)``）。
    """
    if not _PLUGIN_ID.fullmatch(plugin_id):
        raise ValueError(f"插件 id 不合法：{plugin_id!r}")
    return plugin_data_root(base_dir) / plugin_id


__all__: list[str] = ["plugin_data_dir", "plugin_data_root"]
