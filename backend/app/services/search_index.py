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

    async def search_documents(
        self,
        *,
        keyword: str,
        page: int = 1,
        page_size: int = 10,
        category_id: str | None = None,
        author_id: str | None = None,
    ) -> dict[str, Any]:
        """关键词全文检索（对应 TS SearchIndexService.searchDocuments）

        命中 kh_document 的 title / summary / content（IK 分词）；支持按分类 / 作者过滤，
        返回关键词高亮片段（ES 默认 <em> 包裹）。ES 不可用时返回空分页（不抛错）。
        """
        # TS: Math.min(params.pageSize ?? 10, 50) —— schema 已限 50，这里双保险
        page_size = min(page_size, 50)
        from_ = (page - 1) * page_size

        c = es_service.client
        if c is None:
            logger.warning("跳过搜索查询（ES 不可用）")
            return {"items": [], "total": 0, "page": page, "pageSize": page_size}

        await self.ensure_es_index()

        filters: list[dict[str, Any]] = []
        if category_id:
            filters.append({"term": {"categoryId": category_id}})
        if author_id:
            filters.append({"term": {"authorId": author_id}})

        keyword = keyword.strip()
        # title^3 / summary^2：标题、摘要命中比正文权重大；analyzer 与 mapping 的 search_analyzer 一致
        multi_match: dict[str, Any] = {
            "multi_match": {
                "query": keyword,
                "fields": ["title^3", "summary^2", "content"],
                "analyzer": "ik_smart",
            }
        }
        # 有过滤条件才包 bool（filter 只筛不参与打分）；否则直接用 multi_match
        query = (
            {"bool": {"must": [multi_match], "filter": filters}}
            if filters
            else multi_match
        )

        try:
            response = await c.search(
                index=DOCUMENT_INDEX,
                from_=from_,
                size=page_size,
                query=query,
                # 列表不回传全文，仍用 content 做匹配与高亮
                source={"excludes": ["content"]},
                # 命中片段打 <em>，给前端做摘要；正文最多 3 段、标题整段不高亮切片
                highlight={
                    "fields": {
                        "title": {"number_of_fragments": 0},
                        "content": {"fragment_size": 160, "number_of_fragments": 3},
                        "summary": {"fragment_size": 120, "number_of_fragments": 1},
                    }
                },
            )
        except Exception as err:
            logger.warning("搜索查询失败：%s", err)
            return {"items": [], "total": 0, "page": page, "pageSize": page_size}

        # total 可能是 number（旧版）或 {value, relation}（ES8 默认）
        total_raw = response.get("hits", {}).get("total", 0)
        total = total_raw if isinstance(total_raw, int) else (total_raw or {}).get("value", 0)

        items: list[dict[str, Any]] = []
        for hit in response.get("hits", {}).get("hits", []):
            src = hit.get("_source", {}) or {}
            highlight = hit.get("highlight", {}) or {}
            _score = hit.get("_score")
            items.append(
                {
                    "id": str(src.get("id") or hit.get("_id")),
                    "title": src.get("title") or "",
                    "summary": src.get("summary"),
                    "categoryId": src.get("categoryId"),
                    "tags": src.get("tags"),
                    "authorId": src.get("authorId"),
                    "status": src.get("status"),
                    "publishTime": src.get("publishTime"),
                    "score": 0 if _score is None else _score,
                    "highlight": {
                        "title": highlight.get("title", []),
                        "summary": highlight.get("summary", []),
                        "content": highlight.get("content", []),
                    },
                }
            )

        return {"items": items, "total": total, "page": page, "pageSize": page_size}


def _utc_iso() -> str:
    """ES date 字段用的 UTC ISO-8601（对应 TS new Date().toISOString()）"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# 全局单例
search_index_service = SearchIndexService()
