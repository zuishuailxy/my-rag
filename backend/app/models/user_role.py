"""
用户-角色关联模型（PostgreSQL kh_user_role）

已对照 TS src/user/entities/user-role.entity.ts 逐字段核验一致（2026-09-01）：
id / userId / roleId / createdAt。TS 实体未声明 FK 与唯一约束（都在 DDL 里，
TypeORM 同步自数据库）；Python 侧在 ORM 层显式声明（ForeignKey + uk_kh_user_role_user_role
+ idx_kh_user_role_user_id），保证 create_all 语义完整。

⚠️ schema 真相源是 init-scripts/postgresql/init.sql：改字段请先改那里的 DDL 再同步本模型，
应用启动的 create_all 只建不 ALTER（见 main.py lifespan 注释）。

两个易踩的点：
  1. 两个外键 **没有 ON DELETE CASCADE**。删用户必须先删本表的关联行，否则撞
     kh_user_role_user_id_fkey；删角色同理（kh_user_role_role_id_fkey）。
  2. (user_id, role_id) 唯一，重复授予同一角色会撞 uk_kh_user_role_user_role。
     批量授权时可用 ON CONFLICT (user_id, role_id) DO NOTHING 幂等处理。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["UserRole"]


class UserRole(Base):
    """用户-角色关联（kh_user_role）"""

    __tablename__ = "kh_user_role"

    # 关联 ID（雪花，应用层生成）
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # 用户 ID → kh_user.id（无级联删除，删用户前需先解绑）
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("kh_user.id"),
        nullable=False,
    )

    # 角色 ID → kh_role.id（无级联删除，删角色前需先解绑）
    role_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("kh_role.id"),
        nullable=False,
    )

    # 分配时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # ---- 索引与约束 ----
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uk_kh_user_role_user_role"),
        Index("idx_kh_user_role_user_id", "user_id"),
    )
