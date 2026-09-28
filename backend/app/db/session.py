"""
PostgreSQL 异步会话（SQLAlchemy 2.0 + asyncpg）
"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    # 连接池：**必须显式配置**，不能吃默认值。
    # 压测发现：默认（pool_size 5 + max_overflow 10 = 15）在**并发 10~20** 就耗尽 ——
    # `QueuePool limit of size 5 overflow 10 reached, connection timed out`，
    # 表现为 p50 从 0.5s 直接跳到 30s（正好是 pool_timeout），大批请求 500。
    #
    # ⚠️ 容量估算（扩容时按这个算，别拍脑袋）：
    #   每进程上限 = DB_POOL_SIZE + DB_MAX_OVERFLOW
    #   总上限 = 每进程 × uvicorn worker 数 ÷ PG 的 max_connections
    #   默认 20 + 10 = 30，PG 是 100 → **最多 3 个 worker**。加 worker 前先调 PG 的
    #   max_connections 或降 pool_size。
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    # 拿不到连接时最多等多久；超时抛 TimeoutError（压测里就是它变成 500）
    pool_timeout=settings.DB_POOL_TIMEOUT,
    # 回收超过 30 分钟的连接，避免拿到被中间件掐掉的死连接
    pool_recycle=1800,
    # 由 DB_ECHO 单独控制（默认 false）。**刻意不用 settings.DEBUG**：DEBUG 管的是
    # /docs 开关且默认为 True，用它当开关会让每条 SQL 都进日志；而且 SQLAlchemy 的
    # echo 一旦为真就绕过 logging 级别判定（见 app/core/config.py 的 DB_ECHO 注释），
    # 届时靠调 logger 级别或 app/core/logging.py 的降噪名单都压不住。
    echo=settings.DB_ECHO,
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话"""
    async with async_session_factory() as session:
        yield session


async def close_db_engine() -> None:
    """关闭连接池（应用关闭时调用）"""
    await engine.dispose()
