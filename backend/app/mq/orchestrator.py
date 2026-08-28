"""
知识管线编排（对应 TS PipelineOrchestrator）

按本项目 PGVector 决策实现 RAG 重建：分块 → 嵌入 → 写入 PostgreSQL doc_chunk。
TS 侧完整实现（含 KG 建图等）待提供后对齐。
"""
import logging

from app.db.session import async_session_factory
from app.services.indexing import index_document_vectors

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    """文档发布后管线编排"""

    async def handle_rag_reindex(
        self, message_type: str, document_ids: list[str]
    ) -> None:
        """RAG：按文档 ID 重建向量索引（分块 → 向量化 → PGVector doc_chunk）"""
        logger.info("[RAG] type=%s, documentIds=%s", message_type, document_ids)
        for doc_id in document_ids:
            try:
                pk = int(doc_id)
            except (TypeError, ValueError):
                logger.warning("跳过非法文档 ID：%s", doc_id)
                continue
            async with async_session_factory() as session:
                try:
                    await index_document_vectors(session, pk)
                except ValueError as err:
                    logger.warning("文档 %s 重建跳过：%s", doc_id, err)


# 全局单例
pipeline_orchestrator = PipelineOrchestrator()
