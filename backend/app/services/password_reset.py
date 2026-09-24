"""
密码重置验证码

Redis 存储：key = password:reset:code:{email 小写}，值 = 6 位数字验证码，
TTL 10 分钟（RESET_CODE_TTL_SECONDS）。

- 冷却判断不在本服务：sendResetCode 用 getTtl 的剩余秒数判断——
  剩余 > TTL - COOLDOWN 说明距上次发送不足 60 秒，拦截重复发送。
- verify 只比对不删除：重置成功后由调用方显式 delete。
  验证码 10 分钟内可多次尝试、无尝试次数限制——，
  防爆破收敛（错 N 次作废）是潜在增强项，未做。
- email.lower() 只影响 Redis key；PG 侧 findByEmail 不做小写归一。
"""

from app.db.redis import redis_service

RESET_CODE_PREFIX = "password:reset:code:"

# 验证码有效期
RESET_CODE_TTL_SECONDS = 10 * 60

# 发送冷却期
RESET_CODE_COOLDOWN_SECONDS = 60


class PasswordResetService:
    """密码重置验证码管理（单例；依赖全局 redis_service）"""

    @staticmethod
    def _key(email: str) -> str:
        return f"{RESET_CODE_PREFIX}{email.lower()}"

    async def set(self, email: str, code: str) -> None:
        """写入验证码并刷新 TTL"""
        await redis_service.set(self._key(email), code, RESET_CODE_TTL_SECONDS)

    async def get(self, email: str) -> str | None:
        """读取当前验证码；不存在 / 已过期返回 None"""
        return await redis_service.get(self._key(email))

    async def get_ttl(self, email: str) -> int:
        """剩余秒数；缺 key 返回 -2、无 TTL 返回 -1（redis 语义）

        冷却判断依赖它：刚写入时约 600s，随时间递减。
        """
        return await redis_service.ttl(self._key(email))

    async def verify(self, email: str, code: str) -> bool:
        """比对验证码；只比对不删除，删除由调用方负责"""
        stored = await self.get(email)
        return stored is not None and stored == code

    async def delete(self, email: str) -> None:
        """删除验证码"""
        await redis_service.delete(self._key(email))


# 全局单例
password_reset_service = PasswordResetService()
