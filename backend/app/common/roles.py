"""角色编码常量

独立成模块是为了让「角色编码」这一常量与 ORM 模型解耦：
鉴权依赖、服务层（如用户注册默认授予 USER 角色）只需 import 本模块，
不必触碰 SQLAlchemy 模型。
"""

from enum import StrEnum

__all__ = ["RoleCode"]


class RoleCode(StrEnum):
    """角色编码（与 init.sql 种子数据的 role_code 一致）

    StrEnum：成员可直接与普通字符串比较 / 落库。
    """

    ADMIN = "ROLE_ADMIN"
    REVIEWER = "ROLE_REVIEWER"
    USER = "ROLE_USER"
