"""Redis 服务冒烟验证（app/db/redis.py，对应 TS src/redis/redis.service.ts）

逐方法验证 get / set（含 TTL）/ delete / ttl 的语义与 ioredis 版一致：
- 无 TTL 写入 → ttl = -1
- EX 过期写入 → ttl 在 (0, ttl] 内，过期后 get = None、ttl = -2
- delete 返回删除个数；key 不存在 → get = None
- close 后再调用 → 显式 RuntimeError（验证码读写失败必须暴露，不能静默丢）

前置条件：docker 的 redis 运行中（knowledge_hub_redis，端口 6379）。

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_redis.py
"""
import asyncio
import logging

from app.core.config import settings
from app.db.redis import redis_service

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

# 冒烟键统一前缀，结束前全部清掉（冒烟不污染数据）
PREFIX = "smoke:redis:"


def ok(step: str) -> None:
    print(f"  ✅ {step}")


async def main() -> bool:
    all_ok = True

    await redis_service.start()

    try:
        # ---------- 1) 无 TTL 写入 / 读取 ----------
        key1 = f"{PREFIX}plain"
        await redis_service.set(key1, "hello")
        value = await redis_service.get(key1)
        assert value == "hello", f"读取值不符：{value!r}"
        ttl = await redis_service.ttl(key1)
        assert ttl == -1, f"无过期 key 的 ttl 应为 -1，实际 {ttl}"
        ok("set/get 无 TTL 写入读取 + ttl=-1")

        # ---------- 2) 带秒级过期写入 ----------
        key2 = f"{PREFIX}expiring"
        await redis_service.set(key2, "code-123456", ttl_seconds=2)
        ttl = await redis_service.ttl(key2)
        assert 0 < ttl <= 2, f"过期 key 的 ttl 应在 (0, 2]，实际 {ttl}"
        ok(f"set(EX=2) 写入 + ttl={ttl}")

        # ---------- 3) 过期后行为：get=None、ttl=-2 ----------
        await asyncio.sleep(2.2)
        assert await redis_service.get(key2) is None, "过期后应读不到"
        assert await redis_service.ttl(key2) == -2, "不存在 key 的 ttl 应为 -2"
        ok("过期后 get=None + ttl=-2")

        # ---------- 4) delete 返回删除个数 ----------
        n1 = await redis_service.delete(key1)
        n2 = await redis_service.delete(key1)  # 再删一次：0
        assert n1 == 1 and n2 == 0, f"delete 返回值异常：{n1}, {n2}"
        assert await redis_service.get(key1) is None
        ok("delete 1 → 0 + 删除后 get=None")

        # ---------- 5) 不存在的 key 读取 ----------
        assert await redis_service.get(f"{PREFIX}never-exists") is None
        ok("不存在的 key → get=None")

        # ---------- 6) close 后显式报错 ----------
        await redis_service.close()
        try:
            await redis_service.get(f"{PREFIX}after-close")
            raise AssertionError("close 后调用未被拦截")
        except RuntimeError as err:
            assert "Redis 未连接" in str(err)
        ok("close 后调用 → RuntimeError")
    finally:
        # 清理冒烟键（close 已在上面第 6 步调用，重连一次做清理）
        await redis_service.start()
        client = redis_service.client
        assert client is not None
        keys = [k async for k in client.scan_iter(match=f"{PREFIX}*")]
        if keys:
            await redis_service.delete(*keys)
            print(f"  🧹 已清理冒烟键 {len(keys)} 个")
        await redis_service.close()

    print(f"\n全部通过 ✅（Redis {settings.REDIS_HOST}:{settings.REDIS_PORT} db={settings.REDIS_DB}）")
    return all_ok


if __name__ == "__main__":
    passed = asyncio.run(main())
    raise SystemExit(0 if passed else 1)
