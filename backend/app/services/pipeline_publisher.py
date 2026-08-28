"""
文档发布管线投递（对应 TS DocumentPipelinePublisher）

发布后向 RabbitMQ 投递消息，异步触发 RAG / KG / ES 索引消费。
具体 RabbitMQ 客户端实现待提供；此处先定义协议 + 占位实现，
保证发布链路可跑通——真实实现到位后替换 pipeline_publisher 单例即可。

注意：消费侧按本项目 PGVector 决策，RAG 消费者调用
index_document_vectors（写 PostgreSQL doc_chunk），而非 TS 注释里的 ES kh_chunk。
"""
import logging
from typing import Protocol

from app.models.document import DocumentEntity

logger = logging.getLogger(__name__)


class DocumentPipelinePublisher(Protocol):
    """文档发布后向管线（RabbitMQ）投递消息"""

    async def after_publish(self, entity: DocumentEntity) -> None: ...


class NoopPipelinePublisher:
    """占位实现：暂不投递 MQ，仅记日志（待替换为真实 RabbitMQ publisher）"""

    async def after_publish(self, entity: DocumentEntity) -> None:
        logger.warning(
            "DocumentPipelinePublisher 尚未接入 RabbitMQ，跳过投递：documentId=%s",
            entity.id,
        )


# 全局单例（待替换为真实实现）
pipeline_publisher: DocumentPipelinePublisher = NoopPipelinePublisher()
