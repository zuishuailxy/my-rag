"""
用户模型（PostgreSQL kh_user）

（2026-09-01）：
id / username(50) / password(255) / email?(100) / realName?(50) / avatar?(500) /
email_verified(smallint, default 1) / status(smallint, default 1) / lastLoginAt? /
createdAt / updatedAt / deleted(default false)。
；Python ORM 层用 int，
转 string 的职责在 DTO / 响应层（与 document 模型同款约定）。
（见下方 __table_args__），
TypeORM 同步自数据库，Python 侧在 ORM 层显式声明以保证 create_all 语义完整。

REQUIRE_EMAIL_VERIFICATION=false（默认）时注册立即可登录；true 且带邮箱注册时
email_verified 与 status 一起置 0，邮件激活（activateEmail）后恢复 1（services/user.py）。

⚠️ schema 真相源是 init-scripts/postgresql/init.sql：改字段请先改那里的 DDL 再同步本模型，
应用启动的 create_all 只建不 ALTER（见 main.py lifespan 注释）。

两个易踩的点：
  1. 用户名唯一性是 **partial unique index**（uk_kh_user_username，WHERE deleted = false）。
     软删账号不占用用户名，允许同名重新注册；因此应用层判重必须带上 deleted 条件，
     不能只查「是否存在同名行」。
  2. 删除用户前必须先删 kh_user_role，否则撞外键 kh_user_role_user_id_fkey
     （该 FK 没有 ON DELETE CASCADE，见 models/user_role.py）。
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["User"]

# 用户状态：0 禁用 1 启用
#（无枚举），此处保持 int + 模块常量，
USER_STATUS_DISABLED = 0
USER_STATUS_ENABLED = 1


class User(Base):
    """用户（kh_user）"""

    __tablename__ = "kh_user"

    # 用户 ID（雪花，应用层生成）
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # 登录用户名
    username: Mapped[str] = mapped_column(String(50), nullable=False)

    # 密码（bcrypt 哈希，明文绝不落库）
    password: Mapped[str] = mapped_column(String(255), nullable=False)

    # 邮箱（可选）
    email: Mapped[str | None] = mapped_column(String(100))

    # 真实姓名 / 显示名
    real_name: Mapped[str | None] = mapped_column(String(50))

    # 头像 URL
    avatar: Mapped[str | None] = mapped_column(String(500))

    # 邮箱验证状态：0 未验证 1 已验证（默认已验证）。
    # REQUIRE_EMAIL_VERIFICATION=true 的注册分叉会写 0（status 同步置 0），
    # activateEmail 激活时双双恢复 1；存量数据回填为 1（见 init.sql 同步记录）
    email_verified: Mapped[int] = mapped_column(
        SmallInteger,
        default=1,
        server_default="1",
        nullable=False,
    )

    # 状态：0 禁用 1 启用
    status: Mapped[int] = mapped_column(
        SmallInteger,
        default=USER_STATUS_ENABLED,
        server_default="1",
        nullable=False,
    )

    # 最后登录时间
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # 更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # 软删除标记
    deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    # ---- 索引 ----
    # partial unique：只对未删除行生效，软删后同名可再注册
    __table_args__ = (
        Index(
            "uk_kh_user_username",
            "username",
            unique=True,
            postgresql_where="deleted = false",
        ),
    )
