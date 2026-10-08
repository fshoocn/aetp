"""webui 插件的数据模型：插件 UI contribution 声明。

纯数据结构（不依赖 cordis / starlette），供 ``WebUiPlugin`` 与前端清单接口共用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: contribution 种类：独立页面 / 命名插槽 / JS 扩展
UiKind = Literal["page", "slot", "script"]
#: 资源格式：Vue SFC / HTML 片段 / ESM 脚本
UiFormat = Literal["vue", "html", "js"]


@dataclass(frozen=True)
class UiContribution:
    """一项插件前端扩展声明。资源入口是同源 URL，供前端 runtime 加载。"""

    id: str
    kind: UiKind
    format: UiFormat
    owner: str
    entry: str
    path: str | None = None
    title: str | None = None
    menu_group: str | None = None
    menu_icon: str | None = None
    menu_order: float = 0
    target: str | None = None
    order: float = 0

    def to_dict(self) -> dict[str, str | float | None]:
        """返回适合 JSON 清单的普通字典。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "format": self.format,
            "owner": self.owner,
            "entry": self.entry,
            "path": self.path,
            "title": self.title,
            "menu_group": self.menu_group,
            "menu_icon": self.menu_icon,
            "menu_order": self.menu_order,
            "target": self.target,
            "order": self.order,
        }


__all__: list[str] = ["UiContribution", "UiFormat", "UiKind"]
