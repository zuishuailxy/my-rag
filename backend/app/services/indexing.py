"""
RAG 向量索引（对应 TS VectorIndexService，写入 Elasticsearch kh_chunk → 本项目落 PostgreSQL doc_chunk）

职责（与 TS 一一对应）：
- delete_by_doc_id：按 document_id 删除旧块（发布重建 / 下架时调用）
- index_chunks：bulk 写入 / 覆盖 chunk（chunk_id 唯一，ON CONFLICT upsert，对应 ES _id 覆盖）

- load_pipeline_document：Postgres 元数据 + Mongo 正文 → PipelineDocument 快照（对应 TS loadDocumentsByIds + toPipelineDoc）
- reindex_document：单篇重建（对应 TS reindexOne，分块 → 嵌入 → 删旧 → 写新）
- index_document_vectors：按 ID 加载 + 重建的兼容入口，被 DocumentService 与 MQ 消费者调用。

注意：删旧块在嵌入之后执行（有意优化 TS 的先删后建）——新数据就绪才清旧块，
重建中途失败不会留下检索空窗。
"""
import logging
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.mongo import get_document_content_collection
from app.models.document import DocumentEntity, DocumentStatus
from app.models.doc_chunk import DocChunk
from app.pipeline.types import PipelineDocument, to_iso_date
from app.services.chunker import DocumentChunk, chunking_service
from app.services.embedding import embedding_provider
from app.services.es_index import es_chunk_index_service

logger = logging.getLogger("VectorIndexService")


class VectorIndexService:
    """向量索引存储（PG doc_chunk 写入；删除时同步清 ES kh_chunk 关键词侧）"""

    async def delete_by_doc_id(self, session: AsyncSession, document_id: int) -> None:
        """删除某文档全部向量块 + ES 关键词 chunk（发布重建 / 下架时调用）"""
        await session.execute(
            delete(DocChunk).where(DocChunk.document_id == document_id)
        )
        await session.commit()
        # 方案 B：ES kh_chunk 关键词侧同步清理（不可用/失败降级，不抛错）
        await es_chunk_index_service.delete_by_doc_id(document_id)
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
        logger.info("批量索引成功：%d chunks → doc_chunk", len(chunks))


def to_pipeline_document(entity: DocumentEntity, content: str) -> PipelineDocument:
    """Postgres 实体 + Mongo 正文 → 管线统一 DTO（对应 TS toPipelineDoc）"""
    return PipelineDocument(
        id=str(entity.id),
        title=entity.title,
        content=content,
        summary=entity.summary,
        category_id=str(entity.category_id) if entity.category_id is not None else None,
        author_id=str(entity.author_id) if entity.author_id is not None else None,
        team_id=str(entity.team_id) if entity.team_id is not None else None,
        status=int(entity.status),
        tags=entity.tags,
        is_public=entity.is_public,
        view_count=entity.view_count,
        like_count=entity.like_count,
        comment_count=entity.comment_count,
        publish_time=entity.publish_time,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )


async def load_pipeline_document(
    session: AsyncSession, doc_id: int
) -> PipelineDocument | None:
    """按 ID 加载单篇文档快照（不存在 / 已删除返回 None）"""
    entity = await session.get(DocumentEntity, doc_id)
    if entity is None or entity.deleted:
        return None
    content = await _load_content(entity.content_id)
    return to_pipeline_document(entity, content)


async def load_all_published_documents(
    session: AsyncSession,
) -> list[PipelineDocument]:
    """加载全部已发布文档快照（对应 TS loadAllPublishedDocuments，KG BUILD_ALL 用）"""
    result = await session.execute(
        select(DocumentEntity).where(
            DocumentEntity.deleted.is_(False),
            DocumentEntity.status == DocumentStatus.PUBLISHED,
        )
    )
    entities = result.scalars().all()
    docs: list[PipelineDocument] = []
    for entity in entities:
        content = await _load_content(entity.content_id)
        docs.append(to_pipeline_document(entity, content))
    return docs


async def reindex_document(session: AsyncSession, doc: PipelineDocument) -> int:
    """单篇重建（对应 TS reindexOne）：分块 → 批量嵌入 → 删旧 → 写新，返回块数

    删旧块放在嵌入之后（有意优化 TS 的先删后建）：新数据就绪才清旧块，
    重建中途失败不会留下检索空窗。
    """
    if not doc.content or not doc.content.strip():
        logger.warning("文档内容为空，跳过 RAG：documentId=%s", doc.id)
        return 0

    chunks = await chunking_service.chunk(
        content=doc.content,
        document_id=doc.id,
        document_title=doc.title,
        category_id=doc.category_id,
        author_id=doc.author_id,
        team_id=doc.team_id,
        doc_status=doc.status,
        publish_time=to_iso_date(doc.publish_time),
    )
    if not chunks:
        return 0

    vectors = await embedding_provider.embed_texts([c.content for c in chunks])
    for chunk, vec in zip(chunks, vectors):
        chunk.embedding = vec

    svc = VectorIndexService()
    await svc.delete_by_doc_id(session, int(doc.id))   # PG + ES kh_chunk 旧块
    await svc.index_chunks(session, chunks)            # PG 向量（语义）
    await es_chunk_index_service.index_chunks(chunks)  # ES 关键词（IK，方案 B；失败降级）

    logger.info("RAG 索引完成：documentId=%s, chunks=%d", doc.id, len(chunks))
    return len(chunks)


async def index_document_vectors(session: AsyncSession, doc_id: int) -> int:
    """按 ID 加载文档快照并重建（兼容入口：DocumentService.index_document 直调）"""
    doc = await load_pipeline_document(session, doc_id)
    if doc is None:
        raise ValueError(f"文档不存在或已删除: {doc_id}")
    return await reindex_document(session, doc)


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
