"""
认证相关 DTO（对应 TS dto/auth.dto.ts）、登录结果（TS auth.service.ts 的 LoginResult）
与当前登录用户（TS interfaces/auth-user.interface.ts）

命名沿用 TS 语义化重命名约定（QueryReviewTasksDto → ReviewTaskQuery）：
  LoginDto         → LoginRequest
  RegisterDto      → RegisterRequest
  RefreshTokenDto  → RefreshTokenRequest
  AuthUser         → AuthUser（同名，TS 是 interface，此处是 Pydantic 模型）
  LoginResult      → LoginResult（同名；TS 的 tokenType 字面量 'Bearer' → Literal）
  { userId, message } → RegisterResult（TS register 返回的内联类型）

请求体字段 camelCase（username / password 无驼峰差异，refreshToken 有）。

⚠️ 两处刻意优于 TS（均为防 500，TS 侧会由 PG / bcrypt 抛异常变成 5xx）：
  1. username / realName 加了 max_length：kh_user.username 是 VARCHAR(50)、
     real_name 是 VARCHAR(50)，TS 只有 @IsString()，超长会撞 PG 22001 → 500。
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
    "RefreshTokenRequest",
    "RegisterRequest",
    "RegisterResult",
]

# bcrypt 的硬上限（字节）。超过 72 字节 bcrypt 5.0 抛 ValueError，不再静默截断
BCRYPT_MAX_PASSWORD_BYTES = 72


class LoginRequest(BaseModel):
    """登录请求体（对应 TS LoginDto）"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        # 覆盖 Swagger 自动示例：默认会把两个字段都填 "string"，直接 Execute 必失败
        json_schema_extra={"example": {"username": "admin", "password": "123456"}},
    )

    # TS 只有 @IsString()；此处补长度上界，避免超长用户名撞 PG 22001 变 500
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
    """注册请求体（对应 TS RegisterDto）

    简化注册：注册后 status=1，立即可登录（无邮箱验证、无审批流）。
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
    # 密码：TS @MinLength(6)；上限防 bcrypt 72 字节 ValueError（服务层再按字节兜一次）
    password: str = Field(
        ...,
        min_length=6,
        max_length=BCRYPT_MAX_PASSWORD_BYTES,
        description="登录密码（至少 6 位，最长 72 字符）",
    )
    # 邮箱（可选；传了就必须是合法邮箱，空串同样会 422，与 TS @IsEmail 一致）
    email: EmailStr | None = Field(default=None, description="邮箱")
    # 真实姓名 / 显示名（可选）
    real_name: str | None = Field(default=None, max_length=50, description="真实姓名")
    # 角色不在注册时传：注册默认无角色，授权走管理端接口（与 TS 注册流程一致）


class RefreshTokenRequest(BaseModel):
    """刷新 Token 请求体（对应 TS RefreshTokenDto）"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={"example": {"refreshToken": "<登录时返回的 refreshToken>"}},
    )

    # 刷新令牌（服务层校验签名与类型；无效 / 过期返回 401）
    refresh_token: str = Field(..., min_length=1, description="刷新令牌")


class AuthUser(BaseModel):
    """JWT 校验后注入到路由的当前用户（对应 TS interfaces/auth-user.interface.ts）

    由鉴权依赖（二期 AuthService + FastAPI 依赖）从 token 解出后注入，路由里直接用
    `current_user: AuthUser = Depends(...)` 取，等价于 NestJS 的 `@CurrentUser()`。
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
    # 角色编码列表（如 ["ROLE_USER"]；TS 为必填，此处默认空列表，鉴权判断交给依赖层）
    roles: list[str] = Field(default_factory=list)


class LoginResult(BaseModel):
    """登录 / 刷新成功响应（对应 TS auth.service.ts 的 LoginResult）"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 访问令牌（短效，默认 2h）
    access_token: str
    # 刷新令牌（长效，默认 7d；仅用于换新令牌对，服务端不落库）
    refresh_token: str
    # 固定 "Bearer"（TS 字面量类型）
    token_type: Literal["Bearer"] = "Bearer"
    # accessToken 有效秒数（前端据此做静默续期）
    expires_in: int
    # 当前用户信息（与 GET /auth/me 返回结构一致）
    user_info: AuthUser


class RegisterResult(BaseModel):
    """注册成功响应（对应 TS register 返回的内联类型 { userId, message }）"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 新用户 ID
    user_id: str
    # 提示语（固定「注册成功，请登录」）
    message: str
