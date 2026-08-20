"""ORM：测试脚本版本。

脚本文件存本地目录，DB 只存引用与 sha256；同 hash 幂等复用。
(project_pk, name, version) 唯一；parse 相关字段带 CHECK。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from master.domain.enums import ScriptParseLocation, ScriptParseStatus

from .base import Base, JSONType, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .project import Project


class TestScript(Base, TimestampMixin):
    __tablename__ = "test_scripts"
    __table_args__ = (
        UniqueConstraint(
            "project_pk", "name", "version", name="uq_test_scripts_project_name_version"
        ),
        Index("ix_test_scripts_project_created", "project_pk", "created_at"),
        CheckConstraint(
            "parse_status IN ('pending','parsing','parsed','failed')",
            name="ck_test_scripts_parse_status",
        ),
        CheckConstraint(
            "parse_location IN ('master','agent')",
            name="ck_test_scripts_parse_location",
        ),
        CheckConstraint(
            "result_parse_location IN ('master','agent')",
            name="ck_test_scripts_result_parse_location",
        ),
    )

    # sym:id 代理主键（自增 int），仅供内部引用；对外业务标识用 script_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:script_id 脚本业务标识（ULID），全局唯一，对外暴露；文件目录与 HTTP 引用均用它
    script_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sym:project_pk 所属项目代理主键（项目边界 D-12：脚本只能被本项目任务引用）
    project_pk: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # sym:task_type 任务类型（插件类型，如 pytest/cdd/canoe），决定解析与执行插件
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:name 脚本名（项目内可重复，与 version 共同定位版本）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # sym:version 版本号（自 1 递增）；(project_pk, name, version) 唯一
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # sym:file_ref 脚本文件在 Master 本地/对象存储的引用路径（data/scripts/{script_id}/{version}/）
    file_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    # sym:size 脚本包字节数
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # sym:sha256 内容哈希：同 hash 重复上传幂等复用（仓储 get_by_hash）
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:config 插件配置 JSON（插件页面/Schema 表单生成），执行与解析的输入
    config: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # sym:hardware_requirements 硬件能力谓词（§18.5），节点匹配依据
    hardware_requirements: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    # sym:parse_status 用例解析状态（pending/parsing/parsed/failed）
    parse_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ScriptParseStatus.PENDING.value
    )
    # sym:parse_location 用例解析在哪端执行（master=Master 端插件解析；agent=下发到有解析能力的 Agent，D-17）
    parse_location: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ScriptParseLocation.MASTER.value
    )
    # sym:result_parse_location 报告解析在哪端执行（agent=Agent 本地解析后上报；CANoe 类通常为 agent，D-19）
    result_parse_location: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ScriptParseLocation.MASTER.value
    )
    # sym:plugin_version 解析/执行插件版本，Master 校验兼容性（不匹配拒绝派发 PLUGIN_VERSION_MISMATCH）
    plugin_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    # sym:created_by 上传者（users.id），审计字段
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # sym:last_parsed_at 最近一次用例解析完成时间（parse_status 到 parsed 的落点）
    last_parsed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    # sym:project 所属项目 ORM 关系（查询时反查 project_id）
    project: Mapped[Project] = relationship()
