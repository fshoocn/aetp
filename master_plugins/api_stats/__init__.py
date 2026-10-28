"""api-stats 插件包：拦截链 ``observe`` 阶段的热点接口统计示例。

对外契约::

    from plugin import ApiStatsPlugin   # plugin.json 的 entrypoint

    await ctx.plugin(ApiStatsPlugin)    # 依赖 appconfig + webapi
"""

from __future__ import annotations

from .config import ApiStatsConfig
from .plugin import ApiStatsPlugin

__all__: list[str] = ["ApiStatsConfig", "ApiStatsPlugin"]
