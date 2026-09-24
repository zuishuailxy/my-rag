"""权限模块常量

- ADMIN_OPERATION_PERMISSIONS：管理员自动拥有的操作权限码（与 init.sql 里
  kh_permission.permission_code 种子对齐，是 getUserPermissionCodes 把「管理员」身份
  翻译成具体权限码的依据）。
- ADMIN_ROLES：视为管理员的角色编码集合。
- PermissionType：1 菜单 2 按钮 3 接口。
"""

from enum import IntEnum

from app.common.roles import RoleCode

__all__ = ["ADMIN_OPERATION_PERMISSIONS", "ADMIN_ROLES", "PermissionType"]

# 管理员自动拥有的操作权限码
ADMIN_OPERATION_PERMISSIONS: tuple[str, ...] = (
    "document:list",
    "document:create",
    "document:edit",
    "document:delete",
    "document:review",
    "document:category",
    "document:category:query",
    "document:tag",
    "document:version",
    "system:user",
    "system:role",
    "system:permission",
    "system:permission:create",
    "system:permission:edit",
    "system:permission:delete",
    "system:team",
    "system:statistics",
    "system:settings",
    "search",
)

# 视为管理员的角色编码（getUserPermissionCodes 据此追加 ADMIN_OPERATION_PERMISSIONS）
ADMIN_ROLES: tuple[str, ...] = (RoleCode.ADMIN.value,)


class PermissionType(IntEnum):
    """权限类型：1 菜单 2 按钮 3 接口"""

    MENU = 1
    BUTTON = 2
    API = 3
