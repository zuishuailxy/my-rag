"""
文档发布后的知识管线「生产者」（对应 TS DocumentPipelinePublisher）

触发：
- RAG 向量化（rag.reindex.exchange / rag.reindex.by_ids、rag.reindex.delete）
- Search 全文索引（search.index.exchange / search.index.document、search.index.delete）
- KG 建图（kg.graph.exchange）

约定：投递失败只打日志，不回滚文档已发布状态（由 DocumentService 的 try-catch 兜底）。
消息一律只带 documentId，消费者自行查库（Search 曾带 1000 字快照，受队列字符
限制改为按 ID 拉库，对齐 TS 新版）。
"""
import asyncio
import logging
import uuid

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
    SearchIndexMessage,
)
from app.mq.rabbitmq import rabbitmq_service

logger = logging.getLogger("DocumentPipelinePublisher")


class DocumentPipelinePublisher:
    """文档发布后的知识管线生产者"""

    async def after_publish(self, document_id: str | int) -> None:
        """发布成功后调用：并行投递 RAG 分块向量化 + Search 全文索引 + KG 建图任务

        消息只带文档 ID，消费者按 ID 拉库（正文全文在消费侧取，不再走消息）。
        """
        await asyncio.gather(
            self._trigger_rag_reindex(int(document_id)),
            self._trigger_search_index(document_id),
            self._trigger_kg_build(int(document_id)),
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
        """Search：从 kh_document 删除整篇记录（复用 SearchIndexMessage，type=DELETE）"""
        message: SearchIndexMessage = {
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

    async def _trigger_search_index(self, document_id: str | int) -> None:
        """Search：只投 documentId，消费者按 ID 拉库写 ES kh_document"""
        message: SearchIndexMessage = {
            "taskId": str(uuid.uuid4()),
            "type": "INDEX",
            "documentId": str(document_id),
        }
        ok = await rabbitmq_service.publish(
            SEARCH_INDEX_EXCHANGE, SEARCH_RK_INDEX, message
        )
        logger.info(
            "ES 搜索索引%s：documentId=%s, taskId=%s",
            "已投递" if ok else "投递失败",
            document_id,
            message["taskId"],
        )
