"""
文档级全文搜索索引（对应 TS SearchIndexService，写入 Elasticsearch `kh_document`）

与 RAG 向量索引的区别：
- 这里「整篇文档」一条记录（标题 / 摘要 / 正文），给关键词搜索用
- RAG 是「多块 + 向量」（落 PostgreSQL doc_chunk），给语义检索用

<p>仅写入 Elasticsearch；ES 不可用时跳过写入并打日志（不抛错），
避免检索故障拖垮发布链路。</p>

客户端复用 app.db.es 单例（对应 TS 各 service 各自 new Client）。
"""
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from elasticsearch import NotFoundError

from app.db.es import es_service

logger = logging.getLogger("SearchIndexService")

# ES 文档级全文检索索引名
DOCUMENT_INDEX = "kh_document"

# 单节点开发环境：1 分片 0 副本，避免副本未分配导致 yellow
DOCUMENT_INDEX_SETTINGS: dict[str, Any] = {
    "number_of_shards": 1,
    "number_of_replicas": 0,
}

# 字段名保持 camelCase：与 TS DocumentPipelinePublisher.buildSearchIndexData 一致
# （已有索引 / TS 侧数据可平滑复用）。
# 中文正文走 IK 分词（镜像自带 analysis-ik）：索引期细粒度 ik_max_word，查询期 ik_smart。
DOCUMENT_MAPPINGS: dict[str, Any] = {
    "properties": {
        "id": {"type": "keyword"},
        "title": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
        "summary": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
        "content": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
        "tags": {"type": "keyword"},
        "status": {"type": "integer"},
        "isPublic": {"type": "boolean"},
        "categoryId": {"type": "keyword"},
        "authorId": {"type": "keyword"},
        "viewCount": {"type": "integer"},
        "likeCount": {"type": "integer"},
        "commentCount": {"type": "integer"},
        "publishTime": {"type": "date"},
        "createdAt": {"type": "date"},
        "updatedAt": {"type": "date"},
        "indexedAt": {"type": "date"},
    }
}


class SearchIndexService:
    """文档级全文搜索索引（ES kh_document）"""

    async def ensure_es_index(self) -> None:
        """索引不存在则创建 mapping（幂等，对应 TS ensureEsIndex）"""
        await es_service.ensure_index(
            DOCUMENT_INDEX,
            settings_body=DOCUMENT_INDEX_SETTINGS,
            mappings=DOCUMENT_MAPPINGS,
        )

    async def index_document(self, doc: Mapping[str, Any]) -> None:
        """Upsert 一篇文档的搜索记录（id 相同则整条覆盖）

        ES 不可用时只记日志跳过；写入异常向上抛，由调用方（编排器 / 消费者）决定重试。
        """
        c = es_service.client
        if c is None:
            logger.warning("跳过搜索索引写入（ES 不可用）：documentId=%s", doc.get("id"))
            return

        await self.ensure_es_index()
        doc_id = str(doc["id"])
        await c.index(
            index=DOCUMENT_INDEX,
            id=doc_id,
            document={**doc, "indexedAt": _utc_iso()},
            refresh=True,
        )
        logger.info("搜索索引已写入 ES：documentId=%s", doc_id)

    async def delete_document(self, document_id: str) -> None:
        """下架 / 删除时从 ES 移除（404 视为已删除，其余只告警不抛）"""
        c = es_service.client
        if c is None:
            logger.warning("跳过搜索索引删除（ES 不可用）：documentId=%s", document_id)
            return

        doc_id = str(document_id)
        try:
            await c.delete(index=DOCUMENT_INDEX, id=doc_id, refresh=True)
        except NotFoundError:
            logger.debug("ES 文档不存在，跳过删除：documentId=%s", doc_id)
            return
        except Exception as err:
            # 对齐 TS：删除失败不阻断下架流程，只记日志
            logger.warning("ES 删除失败：documentId=%s, %s", doc_id, err)
            return
        logger.info("搜索索引已删除：documentId=%s", doc_id)


def _utc_iso() -> str:
    """ES date 字段用的 UTC ISO-8601（对应 TS new Date().toISOString()）"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# 全局单例
search_index_service = SearchIndexService()
