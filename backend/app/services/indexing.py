"""
RAG 向量索引（对应 TS VectorIndexService，写入 Elasticsearch kh_chunk → 本项目落 PostgreSQL doc_chunk）

职责（与 TS 一一对应）：
- delete_by_doc_id：按 document_id 删除旧块（发布重建 / 下架时调用）
- index_chunks：bulk 写入 / 覆盖 chunk（chunk_id 唯一，ON CONFLICT upsert，对应 ES _id 覆盖）

index_document_vectors：编排入口（拉正文 → 分块 → 嵌入 → 删旧 → 写新），
被 DocumentService.index_document 与 MQ 消费者调用。
"""
import logging
from datetime import datetime

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.mongo import get_document_content_collection
from app.models.document import DocumentEntity
from app.models.doc_chunk import DocChunk
from app.services.chunker import DocumentChunk, chunking_service
from app.services.embedding import embedding_provider

logger = logging.getLogger(__name__)


class VectorIndexService:
    """向量索引存储（写入 PostgreSQL doc_chunk）"""

    async def delete_by_doc_id(self, session: AsyncSession, document_id: int) -> None:
        """删除某文档全部向量块（发布重建 / 下架时调用）"""
        await session.execute(
            delete(DocChunk).where(DocChunk.document_id == document_id)
        )
        await session.commit()
        logger.info("已删除文档向量块：documentId=%s", document_id)

    async def index_chunks(
        self, session: AsyncSession, chunks: list[DocumentChunk]
    ) -> None:
        """批量写入 / 覆盖 chunk（chunk_id 唯一，upsert 对应 ES _id 覆盖）"""
        if not chunks:
            return
        rows = [_chunk_row(c) for c in chunks]
        stmt = pg_insert(DocChunk).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[DocChunk.chunk_id],
            set_={
                "document_id": stmt.excluded.document_id,
                "chunk_index": stmt.excluded.chunk_index,
                "content": stmt.excluded.content,
                "heading": stmt.excluded.heading,
                "document_title": stmt.excluded.document_title,
                "total_chunks": stmt.excluded.total_chunks,
                "category_id": stmt.excluded.category_id,
                "author_id": stmt.excluded.author_id,
                "team_id": stmt.excluded.team_id,
                "doc_status": stmt.excluded.doc_status,
                "publish_time": stmt.excluded.publish_time,
                "embedding": stmt.excluded.embedding,
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)
        await session.commit()
        logger.info("PG 批量索引成功：%d chunks → doc_chunk", len(chunks))


async def index_document_vectors(session: AsyncSession, doc_id: int) -> int:
    """为文档生成向量分片并写入 doc_chunk，返回分片数（幂等：删旧→写新）"""
    entity = await session.get(DocumentEntity, doc_id)
    if entity is None or entity.deleted:
        raise ValueError(f"文档不存在或已删除: {doc_id}")

    content = await _load_content(entity.content_id)
    chunks = await chunking_service.chunk(
        content=content,
        document_id=str(doc_id),
        document_title=entity.title,
        category_id=str(entity.category_id) if entity.category_id is not None else None,
        author_id=str(entity.author_id) if entity.author_id is not None else None,
        team_id=str(entity.team_id) if entity.team_id is not None else None,
        doc_status=int(entity.status) if entity.status is not None else None,
        publish_time=entity.publish_time.isoformat() if entity.publish_time else None,
    )
    if not chunks:
        logger.warning("文档 %s 无可用分块", doc_id)
        return 0

    vectors = await embedding_provider.embed_texts([c.content for c in chunks])
    for chunk, vec in zip(chunks, vectors):
        chunk.embedding = vec

    svc = VectorIndexService()
    await svc.delete_by_doc_id(session, doc_id)
    await svc.index_chunks(session, chunks)
    logger.info("文档 %s 已索引 %d 个分片", doc_id, len(chunks))
    return len(chunks)


def _chunk_row(chunk: DocumentChunk) -> dict:
    """DocumentChunk → doc_chunk 行（对齐 TS buildDocMap）"""
    return {
        "document_id": int(chunk.document_id),
        "chunk_id": chunk.chunk_id,
        "chunk_index": chunk.chunk_index,
        "content": chunk.content,
        "heading": chunk.heading,
        "document_title": chunk.document_title,
        "total_chunks": chunk.total_chunks,
        "category_id": _to_int(chunk.category_id),
        "author_id": _to_int(chunk.author_id),
        "team_id": _to_int(chunk.team_id),
        "doc_status": chunk.doc_status,
        "publish_time": _to_datetime(chunk.publish_time),
        "embedding": chunk.embedding,
    }


def _to_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _load_content(content_id: str) -> str:
    """按 content_id 拉取 Mongo 正文（缺失返回空串）"""
    from bson import ObjectId

    try:
        oid = ObjectId(content_id)
    except Exception:
        return ""
    doc = await get_document_content_collection().find_one({"_id": oid, "deleted": False})
    return doc.get("content", "") if doc else ""


# 全局单例
vector_index_service = VectorIndexService()
