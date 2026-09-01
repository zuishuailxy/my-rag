"""
认证接口（对应 NestJS AuthController）

- POST /auth/register       注册（公开）
- POST /auth/login          登录（公开）
- POST /auth/refresh        刷新令牌（公开）
- GET  /auth/me             当前用户信息（需登录）
- GET  /auth/reviewer-ids   审核员 ID 列表（需 ADMIN / REVIEWER 角色）

状态码：NestJS @Post 默认 201（其余 200），与项目 document 路由约定一致。
鉴权见 app/api/auth_route.py：@public() 对应 @Public()、@roles(...) 对应 @Roles + RolesGuard，
未标记的路由默认需要登录。
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_route import auth_router, public, roles
from app.api.deps import get_current_user
from app.common.roles import RoleCode
from app.db.session import get_db_session
from app.schemas.auth import (
    AuthUser,
    LoginRequest,
    LoginResult,
    RefreshTokenRequest,
    RegisterRequest,
    RegisterResult,
)
from app.services.auth import auth_service

router = auth_router()

_DB = Annotated[AsyncSession, Depends(get_db_session)]
_CurrentUser = Annotated[AuthUser, Depends(get_current_user)]


@router.post("/register", status_code=201, response_model=RegisterResult)
@public()
async def register(dto: RegisterRequest, session: _DB) -> RegisterResult:
    """注册（公开；注册即授 USER 角色，status=1 立即可登录）"""
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


@router.get("/me", response_model=AuthUser)
async def me(user: _CurrentUser, session: _DB) -> AuthUser:
    """当前用户信息（需登录；重新查库，角色与状态为最新）"""
    return await auth_service.get_me(session, user.user_id)


@router.get("/reviewer-ids", response_model=list[str])
@roles(RoleCode.ADMIN, RoleCode.REVIEWER)
async def get_reviewer_ids(session: _DB) -> list[str]:
    """审核员用户 ID 列表（仅 ADMIN / REVIEWER 可访问）"""
    return await auth_service.get_reviewer_ids(session)
