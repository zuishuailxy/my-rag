"""
认证服务

- 签发：HS256 双令牌（access 默认 2h / refresh 默认 7d），
  TokenPayload 完全一致：{ sub, username, type: 'access' | 'refresh' }。
（JWT_ACCESS_EXPIRES / JWT_REFRESH_EXPIRES），
  非法格式 accessExpiresSeconds 回退 7200 秒。
- 注册邮箱验证分叉：REQUIRE_EMAIL_VERIFICATION=true 且带邮箱注册 →
  建未激活用户 + 发激活邮件（发送失败补偿回滚删 token）；verifyEmail 消费令牌激活。
- 密码重置：验证码存 Redis（PasswordResetService，10 分钟有效 / 60 秒发送冷却），
  邮件发送失败补偿回滚删码；verify 只比对，重置成功后由本服务显式删码。
- 服务层无状态、不持会话，session 由路由层传入（同 UserService 惯例）。

（构造器注入 → 模块导入 / settings）：
  userService → app.services.user.user_service
  jwtService → PyJWT（app.core.config.settings.JWT_SECRET）
  config → pydantic-settings 单例 settings
  emailService → app.services.email.email_service
  emailActivation → app.services.email_activation.email_activation_service
  passwordReset → app.services.password_reset.password_reset_service
"""

import logging
import re
import secrets
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
    MessageResult,
    RegisterRequest,
    RegisterResult,
)
from app.services.email import email_service
from app.services.email_activation import email_activation_service
from app.services.password_reset import (
    RESET_CODE_COOLDOWN_SECONDS,
    RESET_CODE_TTL_SECONDS,
    password_reset_service,
)
from app.services.user import user_service

logger = logging.getLogger("AuthService")

# TokenPayload.type 的两个取值
_TOKEN_TYPE_ACCESS = "access"
_TOKEN_TYPE_REFRESH = "refresh"

# 过期时长格式：数字 + s/m/h/d
_DURATION_RE = re.compile(r"^(\d+)([smhd])$")

# 时长单位 → 秒
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration_seconds(raw: str) -> int | None:
    """'2h' → 7200；格式非法返回 None"""
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
        """accessToken 有效秒数；配置格式非法时回退 7200"""
        seconds = _parse_duration_seconds(self._access_expires())
        return seconds if seconds is not None else 7200

    def _require_email_verification(self) -> bool:
        """是否要求注册邮箱激活

'true'；Python 侧 settings 字段本身是 bool（pydantic 解析
        "true"/"1" 等），语义等价。
        """
        return settings.REQUIRE_EMAIL_VERIFICATION

    # ---------------------------------------------------------------- 签发

    def _sign_token(self, user: AuthUser, token_type: str, raw_expires: str) -> str:
        """签发 HS256 JWT，payload（sub / username / type）

；
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
        """登录：校验凭据 → 刷新最后登录时间 → 签发双令牌"""
        user = await user_service.validate_credentials(
            session, dto.username, dto.password
        )
        await user_service.touch_last_login(session, user.user_id)
        return self._build_login_result(user)

    async def register(
        self, session: AsyncSession, dto: RegisterRequest
    ) -> RegisterResult:
        """注册：建用户 + 授 USER 角色

        邮箱验证分叉：需要验证且传了 email → 发激活邮件（失败补偿回滚删 token，
        抛 400「激活邮件发送失败，请稍后再试」），返回体带
        emailVerificationRequired=true；否则返回「注册成功，请登录」。
        """
        result = await user_service.register(
            session, dto, require_email_verification=self._require_email_verification()
        )
        user_id = str(result["userId"])
        need_verify = bool(result["emailVerificationRequired"])

        if need_verify and dto.email:
            token = await email_activation_service.create_token(user_id)
            try:
                await email_service.send_activation_email(
                    str(dto.email), dto.username, token
                )
            except Exception:
                # 邮件发送失败 → 作废刚发的激活令牌
                await email_activation_service.delete_by_token(token)
                raise HTTPException(
                    status_code=400, detail="激活邮件发送失败，请稍后再试"
                )
            return RegisterResult(
                user_id=user_id,
                message="注册成功，请查收邮件激活账户",
                email_verification_required=True,
            )

        return RegisterResult(user_id=user_id, message="注册成功，请登录")

    async def verify_email(self, session: AsyncSession, token: str) -> MessageResult:
        """邮箱激活：消费一次性令牌 → 恢复账户状态

        令牌无效 / 已过期 → 400「激活链接无效或已过期」；用户不存在（激活前被删）
        由 activateEmail 内部 findByIdOrThrow 抛 404「用户不存在」。
        """
        user_id = await email_activation_service.consume_token(token)
        if not user_id:
            raise HTTPException(status_code=400, detail="激活链接无效或已过期")
        message = await user_service.activate_email(session, user_id)
        return MessageResult(message=message)

    async def send_reset_code(self, session: AsyncSession, email: str) -> MessageResult:
        """发送密码重置验证码

        - 邮箱未注册 → 404「该邮箱未注册」
        - 冷却：Redis 剩余 TTL > TTL - COOLDOWN（即距上次发送不足 60s）→ 400
        - 验证码：6 位数字，(100000 + Math.random()*900000)
          一致（100000~999999）；用 secrets.randbelow 取同等随机性更强的实现
        - 发送失败 → 补偿回滚删码，抛 400「邮件发送失败，请稍后再试」
        """
        user = await user_service.find_by_email(session, email)
        if not user:
            raise HTTPException(status_code=404, detail="该邮箱未注册")

        # Redis 剩余 TTL：刚发出去时约 600s。剩余 > 540s 说明距上次发送不足 60s
        ttl = await password_reset_service.get_ttl(email)
        if ttl > RESET_CODE_TTL_SECONDS - RESET_CODE_COOLDOWN_SECONDS:
            raise HTTPException(status_code=400, detail="验证码已发送，请稍后再试")

        code = str(100000 + secrets.randbelow(900000))
        await password_reset_service.set(email, code)
        try:
            await email_service.send_reset_code_email(email, user.username, code)
        except Exception:
            await password_reset_service.delete(email)
            raise HTTPException(status_code=400, detail="邮件发送失败，请稍后再试")
        return MessageResult(message="验证码已发送")

    async def reset_password_by_email(
        self, session: AsyncSession, email: str, code: str, new_password: str
    ) -> MessageResult:
        """按邮箱重置密码

        验证码错误 / 过期 → 400「验证码错误或已过期」；重置成功后删码
。
        """
        if not await password_reset_service.verify(email, code):
            raise HTTPException(status_code=400, detail="验证码错误或已过期")
        await user_service.reset_password_by_email(session, email, new_password)
        await password_reset_service.delete(email)
        return MessageResult(message="密码重置成功，请登录")

    async def refresh(self, session: AsyncSession, refresh_token: str) -> LoginResult:
        """刷新令牌：验签 → 校验 type → 重新签发双令牌

        注意：、无吊销机制——
        旧 refresh token 在过期前始终可用。接入黑名单 / 轮换是后续增强项。
        """
        try:
            payload = jwt.decode(
                refresh_token, settings.JWT_SECRET, algorithms=["HS256"]
            )
        except jwt.PyJWTError:
            # 过期（ExpiredSignatureError）/ 签名不符 / 格式非法统一按此文案，
            # 不泄露具体原因
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
        """当前用户信息"""
        return await user_service.build_auth_user(session, user_id)

    async def build_auth_user(self, session: AsyncSession, user_id: str) -> AuthUser:
        """按 ID 组装 AuthUser"""
        return await user_service.build_auth_user(session, user_id)

    async def get_reviewer_ids(self, session: AsyncSession) -> list[str]:
        """审核员用户 ID 列表"""
        return await user_service.get_user_ids_by_role_code(
            session, RoleCode.REVIEWER
        )


# 全局单例（无状态）
auth_service = AuthService()
