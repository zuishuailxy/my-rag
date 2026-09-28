"""
邮箱激活令牌

双 key 设计（Redis 存储，24 小时有效）：
- token → userId：用户点激活链接时，用 token 查出要激活的账号
- userId → token：同一用户只保留一个有效 token，重发时先按 userId 找到并作废旧的

token 用 secrets.token_hex(32)（toString('hex')，64 个 hex 字符）。
消费即失效：consumeToken 校验通过后两侧 key 一起删除，一次性使用。
"""
import logging
import secrets

from app.db.redis import redis_service

logger = logging.getLogger("EmailActivationService")

TOKEN_PREFIX = "email:activation:token:"
USER_PREFIX = "email:activation:user:"

# 激活令牌有效期
ACTIVATION_TOKEN_TTL_SECONDS = 24 * 3600


class EmailActivationService:
    """邮箱激活令牌管理（单例；依赖全局 redis_service）"""

    @staticmethod
    def _token_key(token: str) -> str:
        return f"{TOKEN_PREFIX}{token}"

    @staticmethod
    def _user_key(user_id: str) -> str:
        return f"{USER_PREFIX}{user_id}"

    async def create_token(self, user_id: str) -> str:
        """为用户生成激活令牌；重发时作废旧令牌"""
        existing = await redis_service.get(self._user_key(user_id))
        if existing:
            await redis_service.delete(self._token_key(existing))
        token = secrets.token_hex(32)
        # token → userId：激活时反查账号
        await redis_service.set(
            self._token_key(token), user_id, ACTIVATION_TOKEN_TTL_SECONDS
        )
        # userId → token：保证同一用户只有一个有效令牌
        await redis_service.set(
            self._user_key(user_id), token, ACTIVATION_TOKEN_TTL_SECONDS
        )
        logger.info("激活令牌已创建：userId=%s, ttl=%s", user_id, ACTIVATION_TOKEN_TTL_SECONDS)
        return token

    async def consume_token(self, token: str) -> str | None:
        """校验并消费令牌，返回 userId；无效 / 已过期返回 None（一次性使用）"""
        user_id = await redis_service.get(self._token_key(token))
        if not user_id:
            return None
        await redis_service.delete(self._token_key(token))
        await redis_service.delete(self._user_key(user_id))
        return user_id

    async def delete_by_token(self, token: str) -> None:
        """按 token 主动作废（如用户重新注册、管理员清理）；token 不存在时静默跳过"""
        user_id = await redis_service.get(self._token_key(token))
        await redis_service.delete(self._token_key(token))
        if user_id:
            await redis_service.delete(self._user_key(user_id))


# 全局单例
email_activation_service = EmailActivationService()
