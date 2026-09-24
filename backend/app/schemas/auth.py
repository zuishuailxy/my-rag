"""
认证相关 DTO、登录结果
与当前登录用户

（QueryReviewTasksDto → ReviewTaskQuery）：
  LoginDto → LoginRequest
  RegisterDto → RegisterRequest
  RefreshTokenDto → RefreshTokenRequest
  AuthUser → AuthUser
  LoginResult → LoginResult
  { userId, message } → RegisterResult

请求体字段 camelCase（username / password 无驼峰差异，refreshToken 有）。

：
  1. username / realName 加了 max_length：kh_user.username 是 VARCHAR(50)、
     real_name 是 VARCHAR(50)，()，超长会撞 PG 22001 → 500。
  2. password 加了 max_length=72：bcrypt 5.0 对 **超过 72 字节** 的密码直接抛
     ValueError（4.x 是静默截断）。字符数 72 只是保守粗筛（中文 3 字节/字，
     24 个汉字就到顶），**服务层仍须按 UTF-8 字节数再校验一次**，超限时：
       - 注册 → 400（不静默截断，避免「用户以为设了 20 位密码其实只有前 24 字节生效」）
       - 登录 → 401（超长密码不可能匹配任何账号，不泄露「格式非法」信息）
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field
from pydantic.alias_generators import to_camel

__all__ = [
    "AuthUser",
    "BCRYPT_MAX_PASSWORD_BYTES",
    "LoginRequest",
    "LoginResult",
    "MessageResult",
    "RefreshTokenRequest",
    "RegisterRequest",
    "RegisterResult",
    "ResetPasswordByEmailRequest",
    "SendResetCodeRequest",
]

# bcrypt 的硬上限（字节）。超过 72 字节 bcrypt 5.0 抛 ValueError，不再静默截断
BCRYPT_MAX_PASSWORD_BYTES = 72


class LoginRequest(BaseModel):
    """登录请求体"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        # 覆盖 Swagger 自动示例：默认会把两个字段都填 "string"，直接 Execute 必失败
        json_schema_extra={"example": {"username": "admin", "password": "123456"}},
    )

    #()；此处补长度上界，避免超长用户名撞 PG 22001 变 500
    username: str = Field(
        ..., min_length=1, max_length=50, description="登录用户名（1-50 字符）"
    )
    # 明文密码，仅应通过 HTTPS 传输；长度上界见模块文档字符串（防 bcrypt ValueError）
    password: str = Field(
        ...,
        min_length=1,
        max_length=BCRYPT_MAX_PASSWORD_BYTES,
        description="登录密码（明文）",
    )


class RegisterRequest(BaseModel):
    """注册请求体

    REQUIRE_EMAIL_VERIFICATION=false（默认）注册后立即可登录；
    true 且传了 email 时需邮件激活（status/email_verified 置 0，激活后恢复）。
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={
            "example": {
                "username": "alice",
                "password": "abc123",
                "email": "alice@example.com",
                "realName": "张三",
            }
        },
    )

    # 用户名（唯一性由 partial unique index uk_kh_user_username 保证，仅约束未软删行）
    username: str = Field(
        ..., min_length=1, max_length=50, description="登录用户名（1-50 字符，不可重复）"
    )
    # 密码：(6)；上限防 bcrypt 72 字节 ValueError（服务层再按字节兜一次）
    password: str = Field(
        ...,
        min_length=6,
        max_length=BCRYPT_MAX_PASSWORD_BYTES,
        description="登录密码（至少 6 位，最长 72 字符）",
    )
    # 邮箱
    email: EmailStr | None = Field(default=None, description="邮箱")
    # 真实姓名 / 显示名（可选）
    real_name: str | None = Field(default=None, max_length=50, description="真实姓名")
    # 角色不在注册时传：注册默认无角色，授权走管理端接口


class RefreshTokenRequest(BaseModel):
    """刷新 Token 请求体"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={"example": {"refreshToken": "<登录时返回的 refreshToken>"}},
    )

    # 刷新令牌（服务层校验签名与类型；无效 / 过期返回 401）
    refresh_token: str = Field(..., min_length=1, description="刷新令牌")


class AuthUser(BaseModel):
    """JWT 校验后注入到路由的当前用户

    由鉴权依赖（二期 AuthService + FastAPI 依赖）从 token 解出后注入，路由里直接用
    `current_user: AuthUser = Depends(...)` 取)`。
    """

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 用户 ID（雪花；转字符串避免 JS Number 精度丢失，与全项目 bigint 约定一致）
    user_id: str
    # 登录用户名
    username: str
    # 真实姓名
    real_name: str | None = None
    # 邮箱
    email: str | None = None
    # 头像 URL
    avatar: str | None = None
    # 角色编码列表
    roles: list[str] = Field(default_factory=list)
    # 合并权限码列表
    permissions: list[str] = Field(default_factory=list)
    # 所在团队 ID 列表（含担任负责人的团队），用于文档检索可见性过滤
    # 由 build_auth_user 查 kh_team_member ∪ kh_team.leader_id 注入
    team_ids: list[str] = Field(default_factory=list)


class LoginResult(BaseModel):
    """登录 / 刷新成功响应"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 访问令牌（短效，默认 2h）
    access_token: str
    # 刷新令牌（长效，默认 7d；仅用于换新令牌对，服务端不落库）
    refresh_token: str
    # 固定 "Bearer"
    token_type: Literal["Bearer"] = "Bearer"
    # accessToken 有效秒数（前端据此做静默续期）
    expires_in: int
    # 当前用户信息（与 GET /auth/me 返回结构一致）
    user_info: AuthUser


class RegisterResult(BaseModel):
    """注册成功响应

    emailVerificationRequired 仅在需要邮件激活时出现；
    Python 侧用 None 表达「不出现」，路由层配 response_model_exclude_none
    使响应里彻底省略该键（而非序列化成 null）。
    """

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 新用户 ID
    user_id: str
    # 提示语（「注册成功，请登录」/「注册成功，请查收邮件激活账户」）
    message: str
    # 需要邮件激活时为 true；免验证路径不返回该键
    email_verification_required: bool | None = Field(
        default=None, description="是否需要邮件激活（仅需要时返回）"
    )


class SendResetCodeRequest(BaseModel):
    """发送密码重置验证码请求体"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={"example": {"email": "alice@example.com"}},
    )

    #()；未注册邮箱由服务层 404
    email: EmailStr = Field(..., description="注册时填写的邮箱")


class ResetPasswordByEmailRequest(BaseModel):
    """按邮箱重置密码请求体"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={
            "example": {
                "email": "alice@example.com",
                "code": "123456",
                "newPassword": "newpass123",
            }
        },
    )

    #()
    email: EmailStr = Field(..., description="注册时填写的邮箱")
    #()（无 MinLength）：空串也放行，交由验证码比对统一 400
    code: str = Field(..., description="邮箱收到的 6 位验证码")
    #(6)；上限见模块文档（防 bcrypt 72 字节 ValueError，服务层再按字节兜一次）
    new_password: str = Field(
        ...,
        min_length=6,
        max_length=BCRYPT_MAX_PASSWORD_BYTES,
        description="新密码（至少 6 位，最长 72 字符）",
    )


class MessageResult(BaseModel):
    """通用消息响应"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    message: str
