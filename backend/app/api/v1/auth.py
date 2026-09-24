"""
认证接口

- POST /auth/register 注册（公开）
- POST /auth/login 登录（公开）
- POST /auth/refresh 刷新令牌（公开）
- GET /auth/verify-email 邮箱激活（公开；激活链接落地页调用）
- POST /auth/password/reset/send-code 发送密码重置验证码（公开）
- POST /auth/password/reset 按邮箱重置密码（公开）
- POST /auth/logout 退出登录（需登录；无状态 JWT，仅返回提示语）
- GET /auth/me 当前用户信息（需登录）
- GET /auth/reviewer-ids 审核员 ID 列表（需 ADMIN / REVIEWER 角色）

状态码：、@Get 200，与项目 document 路由约定一致。
鉴权见 app/api/auth_route.py：@public() 对应 @Public()、@roles(...) 对应 @Roles + RolesGuard，
未标记的路由默认需要登录。
"""

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_route import auth_router, public, roles
from app.api.deps import get_current_user
from app.common.roles import RoleCode
from app.db.session import get_db_session
from app.schemas.auth import (
    AuthUser,
    LoginRequest,
    LoginResult,
    MessageResult,
    RefreshTokenRequest,
    RegisterRequest,
    RegisterResult,
    ResetPasswordByEmailRequest,
    SendResetCodeRequest,
)
from app.services.auth import auth_service

router = auth_router()

_DB = Annotated[AsyncSession, Depends(get_db_session)]
_CurrentUser = Annotated[AuthUser, Depends(get_current_user)]


@router.post(
    "/register",
    status_code=201,
    response_model=RegisterResult,
    # 
    # exclude_none 使免验证路径的响应里彻底省略该键（而非 null）
    response_model_exclude_none=True,
)
@public()
async def register(dto: RegisterRequest, session: _DB) -> RegisterResult:
    """注册（公开；REQUIRE_EMAIL_VERIFICATION=true 且带邮箱时需邮件激活，
    否则注册即授 USER 角色、status=1 立即可登录）"""
    return await auth_service.register(session, dto)


@router.post("/login", status_code=201, response_model=LoginResult)
@public()
async def login(dto: LoginRequest, session: _DB) -> LoginResult:
    """登录（公开；返回双令牌 + 用户信息）"""
    return await auth_service.login(session, dto)


@router.post("/refresh", status_code=201, response_model=LoginResult)
@public()
async def refresh(dto: RefreshTokenRequest, session: _DB) -> LoginResult:
    """刷新令牌（公开；旧双令牌整体换新，refresh token 不落库无吊销）"""
    return await auth_service.refresh(session, dto.refresh_token)


@router.get("/verify-email", response_model=MessageResult)
@public()
async def verify_email(
    # 必填 query 参数：缺失时 FastAPI 返回 422（
    # consumeToken(undefined) → 400，）
    token: Annotated[str, Query(description="激活邮件链接中的令牌")],
    session: _DB,
) -> MessageResult:
    """邮箱激活（公开；消费一次性令牌，幂等：已激活返回「账户已激活，请直接登录」）"""
    return await auth_service.verify_email(session, token)


@router.post("/password/reset/send-code", status_code=201, response_model=MessageResult)
@public()
async def send_reset_code(dto: SendResetCodeRequest, session: _DB) -> MessageResult:
    """发送密码重置验证码（公开；60s 冷却，邮件发送失败自动删码回滚）"""
    return await auth_service.send_reset_code(session, dto.email)


@router.post("/password/reset", status_code=201, response_model=MessageResult)
@public()
async def reset_password_by_email(
    dto: ResetPasswordByEmailRequest, session: _DB
) -> MessageResult:
    """按邮箱重置密码（公开；验证码一次性，成功后失效，需重新登录）"""
    return await auth_service.reset_password_by_email(
        session, dto.email, dto.code, dto.new_password
    )


@router.post("/logout", status_code=201, response_model=MessageResult)
async def logout(user: _CurrentUser) -> MessageResult:
    """退出登录"""
    return MessageResult(message="已退出登录")


@router.get("/me", response_model=AuthUser)
async def me(user: _CurrentUser, session: _DB) -> AuthUser:
    """当前用户信息（需登录；重新查库，角色与状态为最新）"""
    return await auth_service.get_me(session, user.user_id)


@router.get("/reviewer-ids", response_model=list[str])
@roles(RoleCode.ADMIN, RoleCode.REVIEWER)
async def get_reviewer_ids(session: _DB) -> list[str]:
    """审核员用户 ID 列表（仅 ADMIN / REVIEWER 可访问）"""
    return await auth_service.get_reviewer_ids(session)
