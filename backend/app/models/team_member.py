"""
团队-成员关联模型（PostgreSQL kh_team_member）

（2026-09-03）：
id / teamId / userId / memberRole(varchar 20, default 'member') / createdAt。
**无 updatedAt**，不要补。

schema 真相源：init-scripts/postgresql/init.sql。两个外键无 ON DELETE CASCADE，
删团队 / 删用户前需先解绑本表关联（与 kh_user_role 一致）。

Python 侧在 ORM 层显式声明
（ForeignKey + uk_kh_team_member + idx_kh_team_member_user_id），与 user_role.py 同款处理。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["TeamMember"]

# 成员角色
MEMBER_ROLE_LEADER = "leader"
MEMBER_ROLE_MEMBER = "member"


class TeamMember(Base):
    """团队-成员关联（kh_team_member）"""

    __tablename__ = "kh_team_member"

    # 关联 ID（雪花，应用层生成）
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # 团队 ID → kh_team.id（无级联删除，删团队前需先解绑）
    team_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kh_team.id"), nullable=False
    )

    # 用户 ID → kh_user.id（无级联删除，删用户前需先解绑）
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kh_user.id"), nullable=False
    )

    # 成员角色：leader / member
    member_role: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="'member'", default="member"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uk_kh_team_member"),
        Index("idx_kh_team_member_user_id", "user_id"),
    )
