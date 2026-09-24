"""
权限模型（PostgreSQL kh_permission））。
schema 真相源：init-scripts/postgresql/init.sql；应用启动 create_all 只建不 ALTER。

权限类型：1 菜单 2 按钮 3 接口。
permission_code 是运行时校验用的唯一编码（如 document:create、system:user）。

；Python ORM 层用 int
（BigInteger），转 string 的职责在 DTO / 响应层（与 role / user 模型同款约定，
见 app/models/role.py 注释）。DB 列类型必须与 init.sql 的 BIGINT 一致，不能在 ORM 层
改成 String。
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["Permission"]

# 权限类型
PERMISSION_TYPE_MENU = 1
PERMISSION_TYPE_BUTTON = 2
PERMISSION_TYPE_API = 3

# 权限状态：0 禁用 1 启用
PERMISSION_STATUS_DISABLED = 0
PERMISSION_STATUS_ENABLED = 1


class Permission(Base):
    """权限（kh_permission）"""

    __tablename__ = "kh_permission"

    # 权限 ID（雪花，应用层生成）
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # 父权限 ID，0 为根
    parent_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )

    # 权限名称
    permission_name: Mapped[str] = mapped_column(String(50), nullable=False)

    # 权限编码（运行时校验，唯一）
    permission_code: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    # 权限类型：1 菜单 2 按钮 3 接口
    permission_type: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    # 菜单路径
    menu_url: Mapped[str | None] = mapped_column(String(200))

    # 接口 URL 模式
    api_url: Mapped[str | None] = mapped_column(String(500))

    # HTTP 方法
    method: Mapped[str | None] = mapped_column(String(10))

    # 图标
    icon: Mapped[str | None] = mapped_column(String(50))

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
    # onupdate 系推断补齐（2026-09-03）：，
    # 但 ① init.sql 只有 DEFAULT NOW() 无触发器；② 同表族 document/user/team 的
    # updated_at 均为 @UpdateDateColumn。。
    # ⚠️ commit 后读本属性会触发惰性刷新 → MissingGreenlet，先 await session.refresh(实体)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
