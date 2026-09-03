"""
文档发布后管线的 MQ 消费者（对应 TS DocumentPipelineConsumer）

消费：
- RAG 向量化（kh.rag.reindex.queue）
- Search 全文索引 / 清理（kh.search.index.queue）
- KG 知识图谱构建 / 清理（kh.kg.graph.queue）

注册时机：构造时 registerHandler，早于 RabbitMqService.start 的 bindConsumers。
"""
import json
import logging

from aio_pika.abc import AbstractIncomingMessage

from app.mq.constants import (
    KG_GRAPH_QUEUE,
    SEARCH_INDEX_QUEUE,
    RAG_REINDEX_QUEUE,
)
from app.mq.orchestrator import pipeline_orchestrator
from app.mq.rabbitmq import rabbitmq_service

logger = logging.getLogger("DocumentPipelineConsumer")


class DocumentPipelineConsumer:
    """文档发布后管线的 MQ 消费者"""

    def __init__(self, orchestrator=pipeline_orchestrator) -> None:
        self._orchestrator = orchestrator
        # 构造时注册处理器，保证早于 start() 的 bindConsumers
        rabbitmq_service.register_handler(RAG_REINDEX_QUEUE, self.handle_rag)
        rabbitmq_service.register_handler(SEARCH_INDEX_QUEUE, self.handle_search)
        rabbitmq_service.register_handler(KG_GRAPH_QUEUE, self.handle_kg)

    async def handle_rag(self, msg: AbstractIncomingMessage) -> None:
        """RAG：分块 → 向量化 → PG doc_chunk（type 分发在编排器内）"""
        body = parse_json(msg)
        logger.info(
            "[RAG] type=%s, taskId=%s, documentIds=%s",
            body.get("type"),
            body.get("taskId"),
            json.dumps(body.get("documentIds") or [], ensure_ascii=False),
        )
        await self._orchestrator.handle_rag_reindex(
            body.get("type", ""), body.get("documentIds") or []
        )

    async def handle_search(self, msg: AbstractIncomingMessage) -> None:
        """Search：文档级关键词索引（type 分发在编排器内）"""
        body = parse_json(msg)
        logger.info(
            "[Search] type=%s, taskId=%s, documentId=%s",
            body.get("type"),
            body.get("taskId"),
            body.get("documentId"),
        )
        await self._orchestrator.handle_search_index(
            body.get("type", ""),
            str(body.get("documentId", "")),
        )

    async def handle_kg(self, msg: AbstractIncomingMessage) -> None:
        """KG：分块 → 抽实体关系 → Neo4j（type 分发在编排器内）"""
        body = parse_json(msg)
        logger.info(
            "[KG] type=%s, taskId=%s, documentIds=%s",
            body.get("type"),
            body.get("taskId"),
            json.dumps(body.get("documentIds") or [], ensure_ascii=False),
        )
        await self._orchestrator.handle_kg_build(
            body.get("type", ""), body.get("documentIds") or []
        )


def parse_json(msg: AbstractIncomingMessage) -> dict:
    """解析消息体 JSON（解析失败抛错，由包装器 nack）"""
    return json.loads(msg.body.decode("utf-8"))


# 全局单例（构造时已注册处理器）
document_pipeline_consumer = DocumentPipelineConsumer()


def register_consumers() -> None:
    """注册全部 MQ 消费者（幂等；构造时已注册，保留显式入口）"""
    rabbitmq_service.register_handler(
        RAG_REINDEX_QUEUE, document_pipeline_consumer.handle_rag
    )
    rabbitmq_service.register_handler(
        SEARCH_INDEX_QUEUE, document_pipeline_consumer.handle_search
    )
    rabbitmq_service.register_handler(
        KG_GRAPH_QUEUE, document_pipeline_consumer.handle_kg
    )
