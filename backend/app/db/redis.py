"""
Redis 客户端封装

()：客户端随应用启动创建、onModuleDestroy 时 quit；
  Python 侧用全局单例 + main.py lifespan 的 start()/close() 接管（与 es.py 同款生命周期）。
- 方法一一对照：get / set(key, value, ttlSeconds?) / del（Python 里改名 delete，
  因 del 是关键字）/ ttl；onModuleDestroy → close()。

刻意差异（文档化）：
- ioredis 构造即后台建连、maxRetriesPerRequest=3 控制单条命令重试；redis-py 是
  惰性建连、断线后每条命令自动重连，无 per-request 重试上限参数，语义上等价覆盖
  开发场景（Redis 短暂不可用时后续命令自愈），不做额外封装。
- start() 里的 ping 仅健康检查打日志：失败降级为 WARNING 不阻断启动（house 风格，
  与 ES/Neo4j 一致）；后续命令若 Redis 仍不可用会照常抛错，由调用方处理——
  验证码这类数据不能像 ES 索引写入那样静默跳过。
"""
import logging

from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger("RedisService")


class RedisService:
    """Redis 连接管理（单例）"""

    def __init__(self) -> None:
        self._client: Redis | None = None

    @property
    def client(self) -> Redis | None:
        """可用客户端；未 start() 时为 None（调用方据此显式报错而非静默跳过）"""
        return self._client

    async def start(self) -> None:
        """创建客户端 + 健康检查；失败只告警不阻断启动（对齐 es.py 降级风格）"""
        if self._client is not None:
            return
        # password || undefined——空串视为未设密码
        password = settings.REDIS_PASSWORD or None
        self._client = Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            password=password,
            db=settings.REDIS_DB,
            # redis-py 默认返回 bytes；decode_responses=True 才与 ioredis 返回 str 一致
            decode_responses=True,
        )
        try:
            await self._client.ping()
            logger.info(
                "Redis 已连接：host=%s, port=%s, db=%s",
                settings.REDIS_HOST,
                settings.REDIS_PORT,
                settings.REDIS_DB,
            )
        except Exception as err:
            logger.warning("Redis 连接失败（后续命令会自动重连）：%s", err)

    async def close(self) -> None:
        """关闭连接"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, key: str) -> str | None:
        """读取字符串值；key 不存在返回 None"""
        c = self._require_client()
        value = await c.get(key)
        return value if value is None else str(value)

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """写入字符串值；ttl_seconds 非空时以 EX 秒级过期（)`）"""
        c = self._require_client()
        if ttl_seconds is not None:
            await c.set(key, value, ex=ttl_seconds)
            return
        await c.set(key, value)

    async def incr(self, key: str, ttl_seconds: int | None = None) -> int:
        """自增计数并返回新值；首次创建时设过期（滚动窗口计数用）

        过期只在 value==1（本次创建）时设置，避免每次自增都刷新 TTL ——
        否则持续访问的 key 永不过期，日限量就变成了总量限制。
        """
        c = self._require_client()
        value = int(await c.incr(key))
        if ttl_seconds is not None and value == 1:
            await c.expire(key, ttl_seconds)
        return value

    async def delete(self, *keys: str) -> int:
        """删除 key"""
        c = self._require_client()
        return int(await c.delete(*keys))

    async def ttl(self, key: str) -> int:
        """剩余过期秒数：-2 key 不存在；-1 无过期时间（语义与 ioredis/redis-py 一致）"""
        c = self._require_client()
        return int(await c.ttl(key))

    def _require_client(self) -> Redis:
        """未初始化时显式报错——验证码读写失败必须暴露，不能静默丢"""
        if self._client is None:
            raise RuntimeError("Redis 未连接（redis_service.start() 未调用或已关闭）")
        return self._client


# 全局单例（) RedisModule 导出的 RedisService）
redis_service = RedisService()
