"""plugin-manager 的持久化模型：插件安装记录（``aetp_plugin_manager_records``）。

按 ``aetp_{owner}_{logical}`` 命名约定归属 ``plugin-manager``——安装记录是
plugin-manager 的账本（它写它读），db 只按跨插件数据依赖反查消费方声明。
数据仍经 ``ctx.db``（Model DSL / 仓储），符合插件铁律。
"""

from __future__ import annotations

from sqlalchemy import Integer, String

from common.models import Mapped, Model, mapped_column


class InstallRecord(Model):
    """插件安装记录行（物理表 ``aetp_plugin_manager_records``）。

    **标量分列**（name / version / entrypoint / enabled / last_error / digest），
    只有形状各异的配置类字段（config / requires / kind / dependencies）按
    JSON 文本列存放。
    """

    __owner__ = "plugin-manager"
    __tablename__ = "records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[str] = mapped_column(String(64))
    entrypoint: Mapped[str] = mapped_column(String(160))
    resource_root: Mapped[str | None] = mapped_column(String(240), nullable=True)
    enabled: Mapped[int] = mapped_column(Integer(), default=0)
    last_error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    digest: Mapped[str] = mapped_column(String(64), default="")
    config: Mapped[str] = mapped_column(String(4096), default="{}")
    requires: Mapped[str] = mapped_column(String(2048), default="[]")
    kind: Mapped[str] = mapped_column(String(64), default="[]")
    dependencies: Mapped[str] = mapped_column(String(4096), default="[]")


__all__: list[str] = ["InstallRecord"]
