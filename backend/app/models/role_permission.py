"""
角色-权限关联模型（PostgreSQL kh_role_permission）

对应 TS src/user/entities/role-permission.entity.ts（字段逐字对齐）。
schema 真相源：init-scripts/postgresql/init.sql。两个外键无 ON DELETE CASCADE，
删角色 / 删权限前需先解绑本表关联（与 kh_user_role 一致）。

⚠️ TS 实体的 id / roleId / permissionId 经 bigintTransformer 在应用层是 string；
Python ORM 层用 int（BigInteger），转 string 的职责在 DTO / 响应层。
注意：本实体只有 createdAt（CreateDateColumn），无 updatedAt —— 与 DDL 一致。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["RolePermission"]


class RolePermission(Base):
    """角色-权限关联（kh_role_permission）"""

    __tablename__ = "kh_role_permission"

    # 关联 ID（雪花，应用层生成）
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # 角色 ID → kh_role.id（无级联删除，删角色前需先解绑）
    role_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kh_role.id"), nullable=False
    )

    # 权限 ID → kh_permission.id（无级联删除，删权限前需先解绑）
    permission_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kh_permission.id"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uk_kh_role_permission"),
    )
