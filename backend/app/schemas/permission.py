"""权限 DTO 与响应模型

字段逐字对齐 CreatePermissionDto / UpdatePermissionDto / QueryPermissionDto /
AssignPermissionIdsDto；camelCase 别名由 alias_generator=to_camel 处理
（与 role.py 同款约定：请求体与响应体均 camelCase）。

各字符串字段按 init.sql kh_permission 列宽补 max_length
（permission_name 50 / permission_code 100 / menu_url 200 / api_url 500 /
method 10 / icon 50），防超长撞 PG 22001 → 500。

bigint 列：请求体里 parentId / permissionIds 是字符串；
响应体里 id / parentId 一律转字符串，避免 JS Number 精度丢失（全项目约定）。
"""

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

__all__ = [
    "AssignPermissionIdsRequest",
    "CreatePermissionRequest",
    "PermissionPageResponse",
    "PermissionTreeResponse",
    "PermissionVOResponse",
    "QueryPermissionRequest",
    "UpdatePermissionRequest",
]


class CreatePermissionRequest(BaseModel):
    """创建权限请求体"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={
            "example": {
                "permissionName": "新建",
                "permissionCode": "x:create",
                "permissionType": 2,
            }
        },
    )

    permission_name: str = Field(
        ..., min_length=1, max_length=50, description="权限名称（1-50 字符）"
    )
    permission_code: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="权限编码（运行时校验，唯一）",
    )
    # 1 菜单 2 按钮 3 接口
    permission_type: int = Field(..., description="权限类型：1 菜单 2 按钮 3 接口")
    # 父权限 ID（雪花字符串），未传视为根 '0'
    parent_id: str | None = Field(default=None, description="父权限 ID（雪花），默认根 '0'")
    menu_url: str | None = Field(default=None, max_length=200, description="菜单路径")
    api_url: str | None = Field(default=None, max_length=500, description="接口 URL 模式")
    method: str | None = Field(default=None, max_length=10, description="HTTP 方法")
    icon: str | None = Field(default=None, max_length=50, description="图标")
    sort: int | None = Field(default=None, description="排序，默认 0")
    status: int | None = Field(default=None, description="0 禁用 1 启用，默认 1")


class UpdatePermissionRequest(BaseModel):
    """更新权限请求体：status 无取值约束（传 0 即禁用）；parentId 传自身 ID 由服务层拦截 400。
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={"example": {"status": 0}},
    )

    permission_name: str | None = Field(
        default=None, min_length=1, max_length=50, description="权限名称"
    )
    permission_code: str | None = Field(
        default=None, min_length=1, max_length=100, description="权限编码（唯一）"
    )
    permission_type: int | None = Field(default=None, description="1 菜单 2 按钮 3 接口")
    parent_id: str | None = Field(default=None, description="父权限 ID（雪花）")
    menu_url: str | None = Field(default=None, max_length=200, description="菜单路径")
    api_url: str | None = Field(default=None, max_length=500, description="接口 URL 模式")
    method: str | None = Field(default=None, max_length=10, description="HTTP 方法")
    icon: str | None = Field(default=None, max_length=50, description="图标")
    sort: int | None = Field(default=None, description="排序")
    status: int | None = Field(default=None, description="0 禁用 1 启用")


class QueryPermissionRequest(BaseModel):
    """权限分页查询。"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    keyword: str | None = Field(
        default=None, description="模糊匹配 permission_name / permission_code"
    )
    page: int = Field(default=1, ge=1, description="页码，默认 1")
    page_size: int = Field(default=20, ge=1, le=100, description="每页条数，默认 20，上限 100")


class AssignPermissionIdsRequest(BaseModel):
    """分配权限 ID 列表"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={"example": {"permissionIds": ["4000000000000000011"]}},
    )

    # AssignPermissionIdsDto 只有 @IsArray + @IsString({ each: true })，
    # 无 min_length —— 空数组 = 清空全部授权（assignRolePermissions/assignUserPermissions
    # 先删后插，空数组即删除所有关联），故此处刻意不加 min_length。
    permission_ids: list[str] = Field(
        ..., description="权限 ID 列表（雪花字符串）；空数组表示清空"
    )


class PermissionVOResponse(BaseModel):
    """权限响应"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    id: str
    parent_id: str
    permission_name: str
    permission_code: str
    permission_type: int
    menu_url: str | None = None
    api_url: str | None = None
    method: str | None = None
    icon: str | None = None
    sort: int
    status: int


class PermissionPageResponse(BaseModel):
    """权限分页响应"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    items: list[PermissionVOResponse]
    total: int
    page: int
    page_size: int


class PermissionTreeResponse(PermissionVOResponse):
    """权限树节点"""

    children: list["PermissionTreeResponse"] = Field(default_factory=list)
