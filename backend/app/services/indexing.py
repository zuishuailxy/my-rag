"""
发布索引（对应「发布后三个消费者」中的 RAG 消费者）

index_document_vectors：查库拉正文 → 分块 → 嵌入 → 写入 PostgreSQL doc_chunk（PGVector）。
同一文档重复索引时先删旧分片（幂等，支持按文档 ID 重建）。

Search（ES 关键词）与 KG（Neo4j 图谱）消费者见 发布索引链路.md，待对应服务就绪后接入。
"""
import logging

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.mongo import get_document_content_collection
from app.models.document import DocumentEntity
from app.models.doc_chunk import DocChunk
from app.services.chunker import chunk_markdown
from app.services.embedding import embedding_provider

logger = logging.getLogger(__name__)


async def index_document_vectors(session: AsyncSession, doc_id: int) -> int:
    """为文档生成向量分片并写入 doc_chunk，返回分片数（幂等）"""
    entity = await session.get(DocumentEntity, doc_id)
    if entity is None or entity.deleted:
        raise ValueError(f"文档不存在或已删除: {doc_id}")

    content = await _load_content(entity.content_id)
    chunks = chunk_markdown(content)
    if not chunks:
        logger.warning("文档 %s 无可用分块", doc_id)
        return 0

    vectors = await embedding_provider.embed_texts(chunks)

    # 幂等重建：先删该文档旧分片
    await session.execute(delete(DocChunk).where(DocChunk.document_id == doc_id))
    for i, (text, vec) in enumerate(zip(chunks, vectors)):
        session.add(
            DocChunk(document_id=doc_id, chunk_index=i, content=text, embedding=vec)
        )
    await session.commit()

    logger.info("文档 %s 已索引 %d 个分片", doc_id, len(chunks))
    return len(chunks)


async def _load_content(content_id: str) -> str:
    """按 content_id 拉取 Mongo 正文（缺失返回空串）"""
    try:
        from bson import ObjectId
    except ImportError:  # 理论不可达
        return ""
    try:
        oid = ObjectId(content_id)
    except Exception:
        return ""
    doc = await get_document_content_collection().find_one({"_id": oid, "deleted": False})
    return doc.get("content", "") if doc else ""
