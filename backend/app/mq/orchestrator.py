"""
知识管线编排器（对应 TS PipelineOrchestrator）

<p>由 DocumentPipelineConsumer 在消费到 MQ 消息后调用；本类负责
「加载文档快照 → 调具体服务」，不直接碰 RabbitMQ。</p>

- RAG：分块 → 嵌入 → 写入 PostgreSQL doc_chunk（见 services/indexing.py）
- Search：整篇快照 → ES kh_document（见 services/search_index.py）
"""
import logging

from app.db.session import async_session_factory
from app.pipeline.types import PipelineDocument
from app.services.indexing import (
    VectorIndexService,
    load_pipeline_document,
    reindex_document,
)
from app.services.search_index import search_index_service

logger = logging.getLogger("PipelineOrchestrator")


class PipelineOrchestrator:
    """文档发布后管线编排"""

    async def handle_rag_reindex(
        self, message_type: str, document_ids: list[str]
    ) -> None:
        """处理 RAG 消息（对应 TS handleRagReindex）：按 type 分发

        - DELETE_BY_DOC_IDS → 清向量块
        - BY_DOC_IDS → 加载快照 → 逐篇（分块 → 嵌入 → 删旧 → 写新）
        """
        if message_type == "DELETE_BY_DOC_IDS":
            await self.handle_rag_delete(document_ids)
            return

        if message_type != "BY_DOC_IDS" or not document_ids:
            logger.warning("忽略未支持的 RAG 消息：type=%s", message_type)
            return

        docs = await self._load_documents_by_ids(document_ids)
        logger.info("RAG 开始索引：type=%s, total=%d", message_type, len(docs))

        for doc in docs:
            try:
                async with async_session_factory() as session:
                    await reindex_document(session, doc)
            except Exception as err:  # 单篇失败只记日志，不影响同批其余文档（对齐 TS）
                logger.error("RAG 索引失败：documentId=%s, %s", doc.id, err)

    async def handle_rag_delete(self, document_ids: list[str]) -> None:
        """处理 RAG 清理消息：按 ID 列表删除 PG doc_chunk 向量块"""
        for raw_id in document_ids:
            try:
                pk = int(raw_id)
            except (TypeError, ValueError):
                logger.warning("跳过非法文档 ID：%s", raw_id)
                continue
            try:
                async with async_session_factory() as session:
                    await VectorIndexService().delete_by_doc_id(session, pk)
            except Exception as err:  # 单篇失败只记日志，不影响同批其余文档
                logger.error("RAG 向量块清理失败：documentId=%s, %s", raw_id, err)

    # ----------------------------------------------------------------- Search

    async def handle_search_index(
        self, message_type: str, document_id: str, doc: dict
    ) -> None:
        """处理 Search 消息（对应 TS handleSearchIndex）：按 type 分发

        - DELETE → 从 kh_document 移除（向量块由 RAG delete 腿清理）
        - INDEX → 整篇快照 upsert 到 kh_document
        """
        if message_type == "DELETE":
            try:
                await search_index_service.delete_document(document_id)
            except Exception as err:
                logger.error(
                    "搜索索引删除失败：documentId=%s, %s", document_id, err
                )
            return

        if message_type != "INDEX":
            logger.warning("忽略未支持的搜索消息：type=%s", message_type)
            return

        if not doc:
            logger.warning(
                "Search INDEX 消息缺少 document 快照：documentId=%s", document_id
            )
            return

        try:
            await search_index_service.index_document(doc)
        except Exception as err:
            logger.error("搜索索引失败：documentId=%s, %s", document_id, err)

    async def _load_documents_by_ids(self, ids: list[str]) -> list[PipelineDocument]:
        """按 ID 列表加载 Postgres 元数据 + Mongo 正文 → 管线统一快照"""
        result: list[PipelineDocument] = []
        for raw_id in ids:
            try:
                pk = int(raw_id)
            except (TypeError, ValueError):
                logger.warning("跳过非法文档 ID：%s", raw_id)
                continue
            async with async_session_factory() as session:
                doc = await load_pipeline_document(session, pk)
            if doc is None:
                continue
            result.append(doc)
        return result


# 全局单例
pipeline_orchestrator = PipelineOrchestrator()
