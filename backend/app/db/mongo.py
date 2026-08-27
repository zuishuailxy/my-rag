"""
MongoDB 异步客户端（motor）
"""
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

from app.core.config import settings

_client: AsyncIOMotorClient | None = None


def get_mongo_client() -> AsyncIOMotorClient:
    """获取全局 MongoDB 客户端（懒加载单例）"""
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.MONGO_URI)
    return _client


def get_document_content_collection() -> AsyncIOMotorCollection:
    """获取 document_content 集合（文档正文）"""
    return get_mongo_client()[settings.MONGO_DB]["document_content"]


async def close_mongo_client() -> None:
    """关闭 MongoDB 客户端（应用关闭时调用）"""
    global _client
    if _client is not None:
        _client.close()
        _client = None
