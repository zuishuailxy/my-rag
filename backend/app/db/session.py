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
    echo=settings.DEBUG,
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话"""
    async with async_session_factory() as session:
        yield session


async def close_db_engine() -> None:
    """关闭连接池（应用关闭时调用）"""
    await engine.dispose()
