"""
用户-权限关联模型（PostgreSQL kh_user_permission））。
schema 真相源：init-scripts/postgresql/init.sql。两个外键无 ON DELETE CASCADE，
删用户 / 删权限前需先解绑本表关联（与 kh_user_role 一致）。

；
Python ORM 层用 int（BigInteger），转 string 的职责在 DTO / 响应层。
注意：本实体只有 createdAt（CreateDateColumn），无 updatedAt —— 与 DDL 一致。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["UserPermission"]


class UserPermission(Base):
    """用户-权限关联（kh_user_permission）"""

    __tablename__ = "kh_user_permission"

    # 关联 ID（雪花，应用层生成）
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # 用户 ID → kh_user.id（无级联删除，删用户前需先解绑）
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kh_user.id"), nullable=False
    )

    # 权限 ID → kh_permission.id（无级联删除，删权限前需先解绑）
    permission_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kh_permission.id"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "permission_id", name="uk_kh_user_permission"),
    )
