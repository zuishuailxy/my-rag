"""
chunk 关键词索引（方案 B：向量存 PG doc_chunk，关键词 IK 分词补一份到 ES `kh_chunk`）

- index_chunks_to_es：bulk 写入 / 覆盖 chunk（_id = chunk_id，对应 ES _id 覆盖）
- delete_chunks_from_es：按 document_id 删除某文档全部 chunk
- 客户端复用 app.db.es 单例（与 SearchIndexService 同款）；ES 不可用时跳过并记日志，
  不阻塞 RAG（向量仍在 PG）。
"""
import logging
from datetime import datetime, timezone

from app.db.es import es_service
from app.pipeline.types import DocumentChunk

logger = logging.getLogger("EsChunkIndexService")

# chunk 关键词索引名
CHUNK_INDEX = "kh_chunk"

# 单节点开发环境：1 分片 0 副本
CHUNK_INDEX_SETTINGS: dict = {
    "number_of_shards": 1,
    "number_of_replicas": 0,
}

# content/标题走 IK 分词
CHUNK_MAPPINGS: dict = {
    "properties": {
        "chunk_id": {"type": "keyword"},
        "document_id": {"type": "keyword"},
        "document_title": {
            "type": "text",
            "analyzer": "ik_max_word",
            "search_analyzer": "ik_smart",
            "fields": {"keyword": {"type": "keyword"}},
        },
        "content": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
        "heading": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
        "chunk_index": {"type": "integer"},
        "total_chunks": {"type": "integer"},
        "category_id": {"type": "keyword"},
        "author_id": {"type": "keyword"},
        "team_id": {"type": "keyword"},
        # 可见性字段
        "is_public": {"type": "boolean"},
        "doc_status": {"type": "integer"},
        "publish_time": {"type": "date"},
        "indexed_at": {"type": "date"},
    }
}


class EsChunkIndexService:
    """ES kh_chunk（chunk 级关键词）写入管理"""

    async def index_chunks(self, chunks: list[DocumentChunk]) -> bool:
        """bulk 写入 / 覆盖 chunk；ES 不可用或失败返回 False（调用方降级）"""
        if not chunks or not es_service.is_available or es_service.client is None:
            logger.warning(
                "跳过 chunk 关键词写入（ES 不可用）：chunks=%d", len(chunks)
            )
            return False
        client = es_service.client
        await es_service.ensure_index(CHUNK_INDEX, mappings=CHUNK_MAPPINGS)

        operations: list[dict] = []
        for chunk in chunks:
            operations.append({"index": {"_index": CHUNK_INDEX, "_id": chunk.chunk_id}})
            operations.append(self._doc_map(chunk))
        try:
            resp = await client.bulk(operations=operations, refresh=True)
        except Exception as err:
            logger.warning("ES chunk 关键词写入失败（不影响 PG 向量索引）：%s", err)
            return False
        if resp.get("errors"):
            failed = [
                f"{item['index']['_id']}: {item['index'].get('error', {}).get('reason', 'unknown')}"
                for item in resp.get("items", [])
                if item.get("index", {}).get("error")
            ]
            logger.warning("ES chunk 部分写入失败（已降级）：%s", ", ".join(failed))
            return False
        logger.info("ES chunk 关键词写入成功：%d → %s", len(chunks), CHUNK_INDEX)
        return True

    async def delete_by_doc_id(self, document_id: str | int) -> bool:
        """按 document_id 删除某文档全部 chunk；ES 不可用返回 False"""
        if not es_service.is_available or es_service.client is None:
            return False
        client = es_service.client
        try:
            await client.delete_by_query(
                index=CHUNK_INDEX,
                query={"term": {"document_id": str(document_id)}},
                refresh=True,
            )
            return True
        except Exception as err:
            message = str(err)
            if "index_not_found" in message:
                return True # 索引不存在视为已清空
            logger.warning("ES chunk 删除失败：documentId=%s, %s", document_id, message)
            return False

    async def ensure_visibility_mapping(self) -> bool:
        """给已有索引补可见性字段

        老 mapping 没有 is_public（本项目早期只写了 author_id / team_id），
        put_mapping 是增量安全的：已有字段不变，只新增缺失字段。
        ES 不可用 / 失败只记日志返回 False，不阻断发布链路。
        """
        if not es_service.is_available or es_service.client is None:
            return False
        try:
            await es_service.client.indices.put_mapping(
                index=CHUNK_INDEX,
                properties={
                    "is_public": {"type": "boolean"},
                    "author_id": {"type": "keyword"},
                    "team_id": {"type": "keyword"},
                },
            )
            return True
        except Exception as err: # noqa: BLE001 —— 索引不存在等情形均降级
            logger.warning("kh_chunk 可见性 mapping 更新失败：%s", err)
            return False

    async def update_visibility(
        self,
        document_id: str | int,
        is_public: bool,
        team_id: str | int | None,
        author_id: str | int | None,
    ) -> bool:
        """已发布文档只改公开/团队/作者时，批量改 chunk 可见性

        免去重新分块 + 重新嵌入，直接用 update_by_query 就地改三个可见性字段。
        与 PG 侧无关：PG 向量通道按 JOIN kh_document 实时取可见性，无需同步。
        ES 不可用返回 False（调用方降级，不抛错）。
        """
        if not es_service.is_available or es_service.client is None:
            logger.warning(
                "跳过 chunk 可见性更新（ES 不可用）：documentId=%s", document_id
            )
            return False
        try:
            resp = await es_service.client.update_by_query(
                index=CHUNK_INDEX,
                query={"term": {"document_id": str(document_id)}},
                script={
                    "source": (
                        "ctx._source.is_public = params.is_public; "
                        "ctx._source.team_id = params.team_id; "
                        "ctx._source.author_id = params.author_id;"
                    ),
                    "params": {
                        "is_public": bool(is_public),
                        # ES 里空值统一写空串（与 _doc_map 的 `or ""` 一致，便于 term 查询）
                        "team_id": "" if team_id is None else str(team_id),
                        "author_id": "" if author_id is None else str(author_id),
                    },
                },
                refresh=True,
            )
            logger.info(
                "chunk 可见性已更新：documentId=%s, updated=%s",
                document_id,
                resp.get("updated", 0),
            )
            return True
        except Exception as err: # noqa: BLE001
            message = str(err)
            if "index_not_found" in message:
                return True
            logger.warning(
                "chunk 可见性更新失败：documentId=%s, %s", document_id, message
            )
            return False

    @staticmethod
    def _doc_map(chunk: DocumentChunk) -> dict:
        """DocumentChunk → ES 文档（仅关键词侧字段，无 embedding——向量在 PG）"""
        return {
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "document_title": chunk.document_title,
            "content": chunk.content,
            "heading": chunk.heading or "",
            "chunk_index": chunk.chunk_index,
            "total_chunks": chunk.total_chunks,
            "category_id": chunk.category_id or "",
            "author_id": chunk.author_id or "",
            "team_id": chunk.team_id or "",
            "is_public": bool(chunk.is_public),
            "doc_status": chunk.doc_status,
            "publish_time": chunk.publish_time,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }


# 全局单例
es_chunk_index_service = EsChunkIndexService()
