"""
知识管线编排器（对应 TS PipelineOrchestrator）

<p>由 DocumentPipelineConsumer 在消费到 MQ 消息后调用；本类负责
「加载文档快照 → 调具体服务」，不直接碰 RabbitMQ。</p>

- RAG：分块 → 嵌入 → 写入 PostgreSQL doc_chunk（见 services/indexing.py）
- Search：按 documentId 拉库取全文 → ES kh_document（见 services/search_index.py）
- KG：分块 → 抽实体关系 → 写 Neo4j（见 services/graph_build.py）
"""
import logging

from app.db.session import async_session_factory
from app.pipeline.types import PipelineDocument, SearchDocumentData, to_iso_date
from app.services.graph_build import graph_build_service
from app.services.indexing import (
    VectorIndexService,
    load_all_published_documents,
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
        self, message_type: str, document_id: str
    ) -> None:
        """处理 Search 消息（对应 TS handleSearchIndex）：按 type 分发

        - DELETE → 从 kh_document 移除（向量块由 RAG delete 腿清理）
        - INDEX → 按文档 ID 从 Postgres + Mongo 拉全文，写入 ES kh_document
          （消息只带 ID，不再携带正文快照）
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

        docs = await self._load_documents_by_ids([document_id])
        doc = docs[0] if docs else None
        if doc is None:
            logger.warning("Search INDEX 文档不存在：documentId=%s", document_id)
            return

        try:
            await search_index_service.index_document(
                self._to_search_index_doc(doc)
            )
        except Exception as err:
            logger.error("搜索索引失败：documentId=%s, %s", document_id, err)

    # ------------------------------------------------------------------- KG

    async def handle_kg_build(
        self, message_type: str, document_ids: list[str]
    ) -> None:
        """处理 KG 建图消息（对应 TS handleKgBuild）：按 type 分发

        - BUILD_*：读正文 → 分块 → 抽实体关系 → 写 Neo4j
        - DELETE_*：删文档节点及其 chunk / 孤儿实体
        """
        if message_type == "DELETE_BY_DOC_IDS" and document_ids:
            for doc_id in document_ids:
                await graph_build_service.delete_for_document(doc_id)
            return

        if message_type == "BUILD_BY_DOC_IDS" and document_ids:
            docs = await self._load_documents_by_ids(document_ids)
        elif message_type == "BUILD_ALL":
            docs = await self._load_all_published_documents()
        else:
            docs = []

        if not docs:
            logger.warning("忽略未支持或空的 KG 消息：type=%s", message_type)
            return

        logger.info("KG 开始建图：type=%s, total=%d", message_type, len(docs))
        await graph_build_service.build_batch(docs)

    async def _load_all_published_documents(self) -> list[PipelineDocument]:
        """加载全部已发布文档快照（对应 TS loadAllPublishedDocuments）"""
        async with async_session_factory() as session:
            return await load_all_published_documents(session)

    def _to_search_index_doc(self, doc: PipelineDocument) -> SearchDocumentData:
        """Postgres 元数据 + Mongo 全文 → ES kh_document 文档

        对应 TS toSearchIndexDoc；content 取全文（不再截断 1000 字），
        字段名保持 camelCase 与 TS 侧写入的数据同构（已有索引可平滑复用）。
        """
        return SearchDocumentData(
            id=doc.id,
            title=doc.title,
            summary=doc.summary,
            content=doc.content or "",
            categoryId=doc.category_id,
            tags=doc.tags,
            status=doc.status,
            isPublic=doc.is_public,
            viewCount=doc.view_count,
            likeCount=doc.like_count,
            commentCount=doc.comment_count,
            authorId=doc.author_id,
            publishTime=to_iso_date(doc.publish_time),
            createdAt=to_iso_date(doc.created_at),
            updatedAt=to_iso_date(doc.updated_at),
        )

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
