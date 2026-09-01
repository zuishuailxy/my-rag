"""
认证服务（对应 TS src/auth/auth.service.ts）

- 签发：HS256 双令牌（access 默认 2h / refresh 默认 7d），payload 结构与 TS 的
  TokenPayload 完全一致：{ sub, username, type: 'access' | 'refresh' }。
- 过期时长沿用 NestJS 的 <数字><s|m|h|d> 格式（JWT_ACCESS_EXPIRES / JWT_REFRESH_EXPIRES），
  非法格式 accessExpiresSeconds 回退 7200 秒（TS 同款）。
- 服务层无状态、不持会话，session 由路由层传入（同 UserService 惯例）。

与 TS 的对应关系（构造器注入 → 模块导入 / settings）：
  userService  → app.services.user.user_service
  jwtService   → PyJWT（app.core.config.settings.JWT_SECRET）
  config       → pydantic-settings 单例 settings
"""

import logging
import re
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.roles import RoleCode
from app.core.config import settings
from app.schemas.auth import (
    AuthUser,
    LoginRequest,
    LoginResult,
    RegisterRequest,
    RegisterResult,
)
from app.services.user import user_service

logger = logging.getLogger("AuthService")

# TokenPayload.type 的两个取值（TS 字面量 'access' | 'refresh'）
_TOKEN_TYPE_ACCESS = "access"
_TOKEN_TYPE_REFRESH = "refresh"

# 过期时长格式（NestJS TimeSpan 字符串）：数字 + s/m/h/d
_DURATION_RE = re.compile(r"^(\d+)([smhd])$")

# 时长单位 → 秒
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration_seconds(raw: str) -> int | None:
    """'2h' → 7200；格式非法返回 None（调用方决定回退值，对齐 TS 的正则解析）"""
    match = _DURATION_RE.match(raw)
    if not match:
        return None
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


class AuthService:
    """认证服务"""

    # ---------------------------------------------------------------- 配置

    def _access_expires(self) -> str:
        return settings.JWT_ACCESS_EXPIRES

    def _refresh_expires(self) -> str:
        return settings.JWT_REFRESH_EXPIRES

    def _access_expires_seconds(self) -> int:
        """accessToken 有效秒数；配置格式非法时回退 7200（TS 同款兜底）"""
        seconds = _parse_duration_seconds(self._access_expires())
        return seconds if seconds is not None else 7200

    # ---------------------------------------------------------------- 签发

    def _sign_token(self, user: AuthUser, token_type: str, raw_expires: str) -> str:
        """签发 HS256 JWT，payload 与 TS TokenPayload 一致（sub / username / type）

        NestJS jwtService.sign 的 expiresIn 语义 = exp = iat + expiresIn；
        PyJWT 通过在 payload 里显式放 exp（UTC）实现同样效果。
        """
        seconds = _parse_duration_seconds(raw_expires)
        payload = {
            "sub": user.user_id,
            "username": user.username,
            "type": token_type,
            "exp": datetime.now(timezone.utc)
            + timedelta(seconds=seconds if seconds is not None else 7200),
        }
        return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")

    def _sign_access_token(self, user: AuthUser) -> str:
        return self._sign_token(user, _TOKEN_TYPE_ACCESS, self._access_expires())

    def _sign_refresh_token(self, user: AuthUser) -> str:
        return self._sign_token(user, _TOKEN_TYPE_REFRESH, self._refresh_expires())

    def _build_login_result(self, user: AuthUser) -> LoginResult:
        return LoginResult(
            access_token=self._sign_access_token(user),
            refresh_token=self._sign_refresh_token(user),
            token_type="Bearer",
            expires_in=self._access_expires_seconds(),
            user_info=user,
        )

    # ---------------------------------------------------------------- 接口

    async def login(self, session: AsyncSession, dto: LoginRequest) -> LoginResult:
        """登录（对应 TS login）：校验凭据 → 刷新最后登录时间 → 签发双令牌"""
        user = await user_service.validate_credentials(
            session, dto.username, dto.password
        )
        await user_service.touch_last_login(session, user.user_id)
        return self._build_login_result(user)

    async def register(
        self, session: AsyncSession, dto: RegisterRequest
    ) -> RegisterResult:
        """注册（对应 TS register）：建用户 + 授 USER 角色，返回提示语"""
        result = await user_service.register(session, dto)
        return RegisterResult(
            user_id=result["userId"], message="注册成功，请登录"
        )

    async def refresh(self, session: AsyncSession, refresh_token: str) -> LoginResult:
        """刷新令牌（对应 TS refresh）：验签 → 校验 type → 重新签发双令牌

        注意：与 TS 一样 refresh token 服务端**不落库**、无吊销机制——
        旧 refresh token 在过期前始终可用。接入黑名单 / 轮换是后续增强项。
        """
        try:
            payload = jwt.decode(
                refresh_token, settings.JWT_SECRET, algorithms=["HS256"]
            )
        except jwt.PyJWTError:
            # 过期（ExpiredSignatureError）/ 签名不符 / 格式非法统一按此文案，
            # 不泄露具体原因（与 TS catch-all 一致）
            raise HTTPException(
                status_code=401, detail="refresh token 无效或已过期"
            )
        if payload.get("type") != _TOKEN_TYPE_REFRESH:
            raise HTTPException(status_code=401, detail="无效的 refresh token")
        sub = payload.get("sub")
        if not isinstance(sub, str):
            raise HTTPException(status_code=401, detail="无效的 refresh token")
        user = await user_service.build_auth_user(session, sub)
        return self._build_login_result(user)

    async def get_me(self, session: AsyncSession, user_id: str) -> AuthUser:
        """当前用户信息（对应 TS getMe；重新查库取最新状态与角色）"""
        return await user_service.build_auth_user(session, user_id)

    async def build_auth_user(self, session: AsyncSession, user_id: str) -> AuthUser:
        """按 ID 组装 AuthUser（对应 TS buildAuthUser；鉴权依赖复用）"""
        return await user_service.build_auth_user(session, user_id)

    async def get_reviewer_ids(self, session: AsyncSession) -> list[str]:
        """审核员用户 ID 列表（对应 TS getReviewerIds；ADMIN/REVIEWER 角色专用接口）"""
        return await user_service.get_user_ids_by_role_code(
            session, RoleCode.REVIEWER
        )


# 全局单例（无状态）
auth_service = AuthService()
