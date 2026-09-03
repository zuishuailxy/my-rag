"""
团队模型（PostgreSQL kh_team）

已对照 TS src/team/entities/team.entity.ts 逐字段核验一致（2026-09-03）：
id / teamName(100) / teamCode(50)? / description(500)? / leaderId(bigint)? /
parentId(bigint, default 0) / sort(int, default 0) / status(smallint, default 1) /
createdAt / updatedAt / deleted(boolean, default false)。

schema 真相源：init-scripts/postgresql/init.sql。树形结构：parent_id=0 为根，
leader_id → kh_user.id（无外键约束，负责人删除后该列成为悬空引用）。

⚠️ TS 实体的 id / leaderId / parentId 经 bigintTransformer 在应用层是 string；
Python ORM 层用 int（BigInteger），转 string 的职责在响应层（与 role / permission 同款约定）。
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["Team"]

# 团队状态：0 禁用 1 启用
TEAM_STATUS_DISABLED = 0
TEAM_STATUS_ENABLED = 1


class Team(Base):
    """团队（kh_team）"""

    __tablename__ = "kh_team"

    # 团队 ID（雪花，应用层生成）
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # 团队名称
    team_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # 团队编码
    team_code: Mapped[str | None] = mapped_column(String(50))

    # 描述
    description: Mapped[str | None] = mapped_column(String(500))

    # 负责人 → kh_user.id
    leader_id: Mapped[int | None] = mapped_column(BigInteger)

    # 父团队 ID，0 为根
    parent_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )

    sort: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )

    # 状态：0 禁用 1 启用
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="1", default=1
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    # TS @UpdateDateColumn：UPDATE 时由 ORM 自动刷新（init.sql 只有 DEFAULT NOW()，无触发器）
    # ⚠️ 与 kh_user / kh_document 同款：commit 后访问本属性会触发惰性刷新 →
    #    MissingGreenlet（asyncpg），读之前必须先 await session.refresh(实体)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    __table_args__ = (
        # 按父 ID 查子树
        Index("idx_kh_team_parent_id", "parent_id"),
    )
