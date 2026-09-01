"""
文档发布后的知识管线「生产者」（对应 TS DocumentPipelinePublisher）

触发：
- RAG 向量化（rag.reindex.exchange / rag.reindex.by_ids、rag.reindex.delete）
- Search 全文索引（search.index.exchange / search.index.document、search.index.delete）

约定：投递失败只打日志，不回滚文档已发布状态（由 DocumentService 的 try-catch 兜底）。
"""
import asyncio
import logging
import uuid

from app.models.document import DocumentEntity
from app.mq.constants import (
    KG_GRAPH_EXCHANGE,
    KG_RK_BUILD_BY_IDS,
    KG_RK_DELETE,
    SEARCH_INDEX_EXCHANGE,
    RAG_REINDEX_EXCHANGE,
    RAG_RK_BY_IDS,
    RAG_RK_DELETE,
    SEARCH_RK_DELETE,
    SEARCH_RK_INDEX,
)
from app.mq.messages import (
    KgBuildMessage,
    ReindexMessage,
    SearchDeleteMessage,
    SearchIndexMessage,
)
from app.mq.rabbitmq import rabbitmq_service
from app.pipeline.types import SearchDocumentData, to_iso_date

logger = logging.getLogger("DocumentPipelinePublisher")

# Search 消息内正文预览长度上限（与 TS 一致，控制消息体积）
CONTENT_PREVIEW_LIMIT = 1000


def build_search_index_data(
    document: DocumentEntity, content: str | None
) -> SearchDocumentData:
    """发布实体 + Mongo 正文 → kh_document 快照（对应 TS buildSearchIndexData）

    字段名保持 camelCase，与 TS 侧写入的数据同构（已有索引可平滑复用）。
    content 只截前 1000 字作为预览，控制消息体积。
    """
    def s(value: int | None) -> str | None:
        return str(value) if value is not None else None

    content_preview = None
    if content:
        content_preview = (
            content[:CONTENT_PREVIEW_LIMIT]
            if len(content) > CONTENT_PREVIEW_LIMIT
            else content
        )

    return SearchDocumentData(
        id=str(document.id),
        title=document.title,
        summary=document.summary,
        content=content_preview,
        tags=document.tags,
        status=int(document.status) if document.status is not None else 0,
        isPublic=document.is_public,
        categoryId=s(document.category_id),
        authorId=s(document.author_id),
        viewCount=document.view_count,
        likeCount=document.like_count,
        commentCount=document.comment_count,
        publishTime=to_iso_date(document.publish_time),
        createdAt=to_iso_date(document.created_at),
        updatedAt=to_iso_date(document.updated_at),
    )


class DocumentPipelinePublisher:
    """文档发布后的知识管线生产者"""

    async def after_publish(
        self, document: DocumentEntity, content: str | None = None
    ) -> None:
        """发布成功后调用：并行投递 RAG 分块向量化 + Search 全文索引 + KG 建图任务"""
        await asyncio.gather(
            self._trigger_rag_reindex(document.id),
            self._trigger_search_index(document, content),
            self._trigger_kg_build(document.id),
        )

    async def after_unpublish(self, document_id: str | int) -> None:
        """归档 / 删除后：并行通知 RAG / Search / KG 按文档 ID 清理"""
        await asyncio.gather(
            self._trigger_rag_delete(document_id),
            self._trigger_search_delete(document_id),
            self._trigger_kg_delete(document_id),
        )

    async def _trigger_rag_reindex(self, document_id: int) -> None:
        """RAG：按文档 ID 重建向量块"""
        message: ReindexMessage = {
            "taskId": str(uuid.uuid4()),
            "type": "BY_DOC_IDS",
            "documentIds": [str(document_id)],
        }
        ok = await rabbitmq_service.publish(
            RAG_REINDEX_EXCHANGE, RAG_RK_BY_IDS, message
        )
        logger.info(
            "RAG 重建索引%s：documentId=%s, taskId=%s",
            "已投递" if ok else "投递失败",
            document_id,
            message["taskId"],
        )

    async def _trigger_rag_delete(self, document_id: str | int) -> None:
        """RAG：按文档 ID 删除向量块（复用 ReindexMessage，type=DELETE_BY_DOC_IDS）"""
        message: ReindexMessage = {
            "taskId": str(uuid.uuid4()),
            "type": "DELETE_BY_DOC_IDS",
            "documentIds": [str(document_id)],
        }
        ok = await rabbitmq_service.publish(
            RAG_REINDEX_EXCHANGE, RAG_RK_DELETE, message
        )
        logger.info(
            "RAG 向量块清理%s：documentId=%s, taskId=%s",
            "已投递" if ok else "投递失败",
            document_id,
            message["taskId"],
        )

    async def _trigger_kg_build(self, document_id: int) -> None:
        """KG：按文档 ID 建图谱"""
        message: KgBuildMessage = {
            "taskId": str(uuid.uuid4()),
            "type": "BUILD_BY_DOC_IDS",
            "documentIds": [str(document_id)],
        }
        ok = await rabbitmq_service.publish(
            KG_GRAPH_EXCHANGE, KG_RK_BUILD_BY_IDS, message
        )
        logger.info(
            "KG 建图%s：documentId=%s, taskId=%s",
            "已投递" if ok else "投递失败",
            document_id,
            message["taskId"],
        )

    async def _trigger_kg_delete(self, document_id: str | int) -> None:
        """KG：按文档 ID 删除图谱"""
        message: KgBuildMessage = {
            "taskId": str(uuid.uuid4()),
            "type": "DELETE_BY_DOC_IDS",
            "documentIds": [str(document_id)],
        }
        ok = await rabbitmq_service.publish(
            KG_GRAPH_EXCHANGE, KG_RK_DELETE, message
        )
        logger.info(
            "KG 图谱清理%s：documentId=%s, taskId=%s",
            "已投递" if ok else "投递失败",
            document_id,
            message["taskId"],
        )

    async def _trigger_search_delete(self, document_id: str | int) -> None:
        """Search：从 kh_document 删除整篇记录"""
        message: SearchDeleteMessage = {
            "taskId": str(uuid.uuid4()),
            "type": "DELETE",
            "documentId": str(document_id),
        }
        ok = await rabbitmq_service.publish(
            SEARCH_INDEX_EXCHANGE, SEARCH_RK_DELETE, message
        )
        logger.info(
            "Search 索引清理%s：documentId=%s, taskId=%s",
            "已投递" if ok else "投递失败",
            document_id,
            message["taskId"],
        )

    async def _trigger_search_index(
        self, document: DocumentEntity, content: str | None
    ) -> None:
        """Search：消息内直接带文档快照，消费者无需再查库即可写索引"""
        message: SearchIndexMessage = {
            "taskId": str(uuid.uuid4()),
            "type": "INDEX",
            "documentId": str(document.id),
            "document": build_search_index_data(document, content),
        }
        ok = await rabbitmq_service.publish(
            SEARCH_INDEX_EXCHANGE, SEARCH_RK_INDEX, message
        )
        logger.info(
            "ES 搜索索引%s：documentId=%s, taskId=%s",
            "已投递" if ok else "投递失败",
            document.id,
            message["taskId"],
        )
