"""
角色模型（PostgreSQL kh_role）

（2026-09-01）：
id / roleName / roleCode(unique) / description?(200) / status(smallint, default 1)。
；Python ORM 层用 int，
转 string 的职责在 DTO / 响应层（与 document 模型同款约定）。

⚠️ schema 真相源是 init-scripts/postgresql/init.sql：改字段请先改那里的 DDL 再同步本模型，
应用启动的 create_all 只建不 ALTER（见 main.py lifespan 注释）。

注意：本表 **没有** created_at / updated_at / deleted，与 kh_user 不对称。若要支持角色软删。
"""

from sqlalchemy import BigInteger, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.common.roles import RoleCode
from app.models.base import Base

__all__ = ["Role", "RoleCode"]

# 角色状态：0 禁用 1 启用
#（无枚举），此处保持 int + 模块常量，
ROLE_STATUS_DISABLED = 0
ROLE_STATUS_ENABLED = 1

# 预置角色编码
ROLE_ADMIN = RoleCode.ADMIN
ROLE_REVIEWER = RoleCode.REVIEWER
ROLE_USER = RoleCode.USER


class Role(Base):
    """角色（kh_role）"""

    __tablename__ = "kh_role"

    # 角色 ID（雪花，应用层生成）
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # 角色名称（展示用）
    role_name: Mapped[str] = mapped_column(String(50), nullable=False)

    # 角色编码，如 ROLE_ADMIN / ROLE_REVIEWER / ROLE_USER
    role_code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)

    # 角色描述
    description: Mapped[str | None] = mapped_column(String(200))

    # 状态：0 禁用 1 启用
    status: Mapped[int] = mapped_column(
        SmallInteger,
        default=ROLE_STATUS_ENABLED,
        server_default="1",
        nullable=False,
    )
