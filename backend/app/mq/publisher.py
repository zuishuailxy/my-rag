"""
文档发布后的知识管线「生产者」（对应 TS DocumentPipelinePublisher）

触发：RAG 向量化。
约定：投递失败只打日志，不回滚文档已发布状态。
"""
import logging
import uuid

from app.mq.constants import RAG_REINDEX_EXCHANGE, RAG_RK_BY_IDS
from app.mq.messages import ReindexMessage
from app.mq.rabbitmq import rabbitmq_service
from app.models.document import DocumentEntity

logger = logging.getLogger(__name__)


class DocumentPipelinePublisher:
    """文档发布后的知识管线生产者"""

    async def after_publish(self, document: DocumentEntity) -> None:
        """发布成功后调用：投递 RAG 分块向量化任务"""
        await self._trigger_rag_reindex(document.id)

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
