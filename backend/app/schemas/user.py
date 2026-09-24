"""用户管理 DTO

来源与命名对照：
  UpdateUserDto → UserUpdateRequest
  QueryUserDto → UserPageQuery
  UpdateProfileDto → ProfileUpdateRequest
  ChangePasswordDto → ChangePasswordRequest
  ResetPasswordDto → ResetPasswordRequest
  AssignRolesDto → AssignRolesRequest
  vo/user.vo.ts → UserVOResponse（+ 分页 / 角色码 / 统计响应）

（防 PG 22001 → 500，与 role 模块同款）：
()，超长会撞 PG 列宽变 500；
此处按 kh_user 列宽补 max_length（email 100 / realName 50 / avatar 500）。

⚠️ 422 vs 400：，
FastAPI 返回 **422**——本项目沿用先例，
语义等价，不再为对齐状态码引入自定义异常处理。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field
from pydantic.alias_generators import to_camel

from app.schemas.auth import BCRYPT_MAX_PASSWORD_BYTES

__all__ = [
    "AssignRolesRequest",
    "ChangePasswordRequest",
    "CreateUserRequest",
    "ProfileUpdateRequest",
    "ResetPasswordRequest",
    "UserPageQuery",
    "UpdateUserRequest",
    "UserPermissionCodesResponse",
    "UserPermissionIdsResponse",
    "UserRoleCodesResponse",
    "UserStatsResponse",
    "UserVOResponse",
]


class CreateUserRequest(BaseModel):
    """管理员创建用户：password 仅 @MinLength(6)（max_length=72 是字符数粗筛，
    服务层再按 UTF-8 字节数兜底，同 register/changePassword 先例）；
    status 无取值约束，默认 1 由服务层处理；roleCodes 默认 ROLE_USER
    也由服务层处理。
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={
            "example": {
                "username": "bob",
                "password": "123456",
                "email": "bob@example.com",
                "realName": "张三",
                "status": 1,
                "roleCodes": ["ROLE_USER"],
            }
        },
    )

    username: str = Field(..., description="用户名")
    password: str = Field(
        ...,
        min_length=6,
        max_length=BCRYPT_MAX_PASSWORD_BYTES,
        description="密码（至少 6 位，最长 72 字符）",
    )
    email: EmailStr | None = Field(default=None, max_length=100, description="邮箱")
    real_name: str | None = Field(default=None, max_length=50, description="真实姓名")
    avatar: str | None = Field(default=None, max_length=500, description="头像 URL")
    status: int | None = Field(default=None, description="状态：0 禁用 1 启用，默认 1")
    role_codes: list[str] | None = Field(
        default=None, description="角色编码列表，缺省授予 ROLE_USER"
    )


class UpdateUserRequest(BaseModel):
    """管理员更新用户：status 无取值约束（任意数字都接受）；email 不做唯一性校验
。
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={
            "example": {"email": "user@example.com", "realName": "张三", "status": 1}
        },
    )

    email: EmailStr | None = Field(default=None, max_length=100, description="邮箱")
    real_name: str | None = Field(default=None, max_length=50, description="真实姓名")
    avatar: str | None = Field(default=None, max_length=500, description="头像 URL")
    status: int | None = Field(default=None, description="状态：0 禁用 1 启用")


class ProfileUpdateRequest(BaseModel):
    """当前用户更新自己资料"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={"example": {"realName": "张三", "avatar": "https://example.com/a.png"}},
    )

    email: EmailStr | None = Field(default=None, max_length=100, description="邮箱")
    real_name: str | None = Field(default=None, max_length=50, description="真实姓名")
    avatar: str | None = Field(default=None, max_length=500, description="头像 URL")


class ChangePasswordRequest(BaseModel):
    """修改密码请求体

    newPassword MinLength(6)；max_length=72 是字符数粗筛，
    服务层再按 UTF-8 字节数兜底（bcrypt 上限，见 services/user.py docstring）。
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={"example": {"oldPassword": "123456", "newPassword": "new-pass-6"}},
    )

    old_password: str = Field(..., min_length=1, description="原密码")
    new_password: str = Field(..., min_length=6, max_length=72, description="新密码（6-72 字符）")


class ResetPasswordRequest(BaseModel):
    """管理员重置密码请求体"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={"example": {"newPassword": "reset-pass-6"}},
    )

    new_password: str = Field(..., min_length=6, max_length=72, description="新密码（6-72 字符）")


class AssignRolesRequest(BaseModel):
    """全量替换用户角色

    @ArrayNotEmpty → min_length=1：不允许空数组（清空全部角色不走此接口）。
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={"example": {"roleCodes": ["ROLE_REVIEWER", "ROLE_USER"]}},
    )

    role_codes: list[str] = Field(..., min_length=1, description="角色编码列表（全量替换）")


class UserPageQuery(BaseModel):
    """用户分页查询（；由路由层逐参数 Query() 组装）"""

    keyword: str | None = Field(default=None, description="用户名/姓名/邮箱 模糊匹配")
    role_code: str | None = Field(default=None, description="按角色编码筛选，如 ROLE_REVIEWER")
    status: int | None = Field(default=None, description="状态：0 禁用 1 启用")
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(default=20, ge=1, le=100, description="每页条数（1-100）")


class UserVOResponse(BaseModel):
    """用户对外展示

    时间字段经 document_to_dict 同款 isoformat 口径（naive UTC，无时区后缀）。
    """

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 用户 ID（雪花；转字符串避免 JS Number 精度丢失，与全项目 bigint 约定一致）
    id: str
    username: str
    email: str | None = None
    real_name: str | None = None
    avatar: str | None = None
    # 状态：0 禁用 1 启用
    status: int
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # 全部**启用**角色编码（kh_role.status=1 过滤后）
    role_codes: list[str]


class UserPageResponse(BaseModel):
    """用户分页响应"""

    items: list[UserVOResponse]
    total: int
    page: int
    page_size: int = Field(alias="pageSize")

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class UserRoleCodesResponse(BaseModel):
    """用户角色码响应"""

    user_id: str = Field(alias="userId")
    role_codes: list[str] = Field(alias="roleCodes")

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class UserPermissionCodesResponse(BaseModel):
    """用户权限码响应

    permissionCodes = 合并后的全部权限码（直接 + 角色）；directPermissionIds
    = 用户直接授权（非经角色）的权限 ID 列表。
    """

    user_id: str = Field(alias="userId")
    permission_codes: list[str] = Field(alias="permissionCodes")
    direct_permission_ids: list[str] = Field(alias="directPermissionIds")


class UserPermissionIdsResponse(BaseModel):
    """用户直接授权 ID 响应"""

    user_id: str = Field(alias="userId")
    permission_ids: list[str] = Field(alias="permissionIds")


class UserStatsResponse(BaseModel):
    """用户统计响应"""

    document_count: int = Field(alias="documentCount", description="文档总数（未删除）")
    view_count: int = Field(alias="viewCount", description="浏览量合计")
    like_count: int = Field(alias="likeCount", description="点赞量合计")
    comment_count: int = Field(alias="commentCount", description="评论量合计")

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)
