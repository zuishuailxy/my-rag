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
