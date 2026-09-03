"""角色管理 DTO（对应 TS user/dto/extra.dto.ts 的 CreateRoleDto / UpdateRoleDto）

命名沿用 TS 语义化重命名约定：
  CreateRoleDto → RoleCreateRequest
  UpdateRoleDto → RoleUpdateRequest
  控制器内联返回对象 → RoleBriefResponse（不含 status，create/list 用）
                     / RoleDetailResponse（含 status，get/update 用）

请求/响应字段 camelCase（roleName / roleCode 有驼峰差异）。

⚠️ 刻意优于 TS：TS 各字段只有 @IsString()，超长会撞 PG 22001 变 500；
此处按 kh_role 列宽补 max_length（roleName/roleCode 50、description 200）。
"""

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

__all__ = [
    "RoleBriefResponse",
    "RoleCreateRequest",
    "RoleDetailResponse",
    "RolePermissionIdsResponse",
    "RoleUpdateRequest",
]


class RoleCreateRequest(BaseModel):
    """创建角色请求体（对应 TS CreateRoleDto；description 可选默认 None）"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={
            "example": {
                "roleName": "运营",
                "roleCode": "ROLE_OPERATOR",
                "description": "运营后台权限",
            }
        },
    )

    # 角色名称（展示用）
    role_name: str = Field(..., min_length=1, max_length=50, description="角色名称（1-50 字符）")
    # 角色编码，唯一性由 kh_role.role_code 唯一约束 + 服务层 409 查重保证
    role_code: str = Field(..., min_length=1, max_length=50, description="角色编码（如 ROLE_OPERATOR，不可重复）")
    # 角色描述（可选）
    description: str | None = Field(default=None, max_length=200, description="角色描述")


class RoleUpdateRequest(BaseModel):
    """更新角色请求体（对应 TS UpdateRoleDto；全字段可选，未传不改动）

    与 TS 一致：status 无取值约束（任意数字都接受，传 0 即禁用）。
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={
            "example": {"roleName": "运营（改）", "description": "更新后的描述", "status": 1}
        },
    )

    role_name: str | None = Field(default=None, min_length=1, max_length=50, description="角色名称")
    description: str | None = Field(default=None, max_length=200, description="角色描述")
    status: int | None = Field(default=None, description="状态：0 禁用 1 启用")


class RoleBriefResponse(BaseModel):
    """角色简要响应（对应 TS create / listRoles 的内联返回对象，不含 status）"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 角色 ID（雪花；转字符串避免 JS Number 精度丢失，与全项目 bigint 约定一致）
    id: str
    # 角色名称
    role_name: str
    # 角色编码
    role_code: str
    # 角色描述
    description: str | None = None


class RoleDetailResponse(RoleBriefResponse):
    """角色完整响应（对应 TS getRole / updateRole 的内联返回对象，多 status）"""

    # 状态：0 禁用 1 启用
    status: int


class RolePermissionIdsResponse(BaseModel):
    """角色已授权权限 ID 列表（对应 TS getRolePermissions / assignRolePermissions 的
    内联返回 { roleId, permissionIds }）"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    role_id: str
    permission_ids: list[str]
