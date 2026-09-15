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

from app.common.document_access import (
    DOC_VISIBILITY_FIELDS,
    DocumentAccessScope,
    empty_scope,
    es_visibility_filter,
)
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
        # 可见性过滤用（公开 ∪ 本人 ∪ 所在团队）；必须显式 keyword，
        # 否则动态映射会把它建成 text，`terms` 查询语义会不对（对齐 TS mapping）
        "teamId": {"type": "keyword"},
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

    async def ensure_visibility_mapping(self) -> bool:
        """给已有 kh_document 索引补可见性字段（增量、幂等；对齐 chunk 侧同名方法）

        为什么需要：ensure_es_index 只在索引不存在时建 mapping，早于本次可见性改造
        创建的索引没有 teamId。若放任 ES 动态映射，先写入的字符串会被建成 text，
        而可见性过滤用 `terms` 直查该字段 —— text 按分词后的词项匹配，
        与 keyword 精确匹配语义不同，会让团队可见性静默失效。故显式 put_mapping。

        ES 不可用 / 失败只记日志返回 False，不阻断启动。
        """
        if not es_service.is_available or es_service.client is None:
            logger.warning("跳过 kh_document 可见性 mapping 更新（ES 不可用）")
            return False
        try:
            await es_service.client.indices.put_mapping(
                index=DOCUMENT_INDEX,
                properties={
                    "isPublic": {"type": "boolean"},
                    "authorId": {"type": "keyword"},
                    "teamId": {"type": "keyword"},
                },
            )
            return True
        except Exception as err:  # noqa: BLE001 —— 索引不存在等情形均降级
            logger.warning("kh_document 可见性 mapping 更新失败：%s", err)
            return False

    async def update_visibility(
        self,
        document_id: str | int,
        is_public: bool,
        team_id: str | int | None,
        author_id: str | int | None,
    ) -> bool:
        """已发布文档只改公开 / 团队 / 作者时，就地改 kh_document 的可见性字段

        chunk 侧 es_chunk_index_service.update_visibility 的文档级版本：文档由公开转
        私有后若不同步，未授权用户仍能通过 POST /search 搜到该文档的标题与摘要。
        按 _id（= 文档 ID）update_by_query 就地改，免去从 PG 重拉全文重灌。

        与 chunk 侧一致：空值写空串（便于 term 查询），ES 不可用/失败返回 False 不抛错。
        """
        if not es_service.is_available or es_service.client is None:
            logger.warning(
                "跳过文档可见性更新（ES 不可用）：documentId=%s", document_id
            )
            return False
        try:
            resp = await es_service.client.update_by_query(
                index=DOCUMENT_INDEX,
                # _id 即文档 ID（index_document 以 doc_id 作 _id），只改这一条
                query={"ids": {"values": [str(document_id)]}},
                script={
                    "source": (
                        "ctx._source.isPublic = params.isPublic; "
                        "ctx._source.teamId = params.teamId; "
                        "ctx._source.authorId = params.authorId;"
                    ),
                    "params": {
                        "isPublic": bool(is_public),
                        "teamId": "" if team_id is None else str(team_id),
                        "authorId": "" if author_id is None else str(author_id),
                    },
                },
                refresh=True,
            )
            logger.info(
                "文档可见性已更新：documentId=%s, updated=%s",
                document_id,
                resp.get("updated", 0),
            )
            return True
        except Exception as err:  # noqa: BLE001
            if "index_not_found" in str(err):
                return True
            logger.warning(
                "文档可见性更新失败：documentId=%s, %s", document_id, err
            )
            return False

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
        scope: DocumentAccessScope | None = None,
    ) -> dict[str, Any]:
        """关键词全文检索（对应 TS SearchIndexService.searchDocuments）

        命中 kh_document 的 title / summary / content（IK 分词）；支持按分类 / 作者过滤，
        返回关键词高亮片段（ES 默认 <em> 包裹）。ES 不可用时返回空分页（不抛错）。

        可见性（对齐 chunk 检索）：公开 ∪ 本人 ∪ 所在团队，命中任一可读；
        ADMIN / REVIEWER 的 scope.unrestricted 时不加过滤。scope 缺省按
        empty_scope 兜底（只能看公开），**不会**退化成不过滤。
        """
        # 可见性字段取 DOC_VISIBILITY_FIELDS（camelCase），与 TS ES_DOC_VISIBILITY_FIELDS 对应。
        # 字段缺失或未回填的历史记录会因 term 不命中而对外不可见（偏保守，不泄露）。
        scope = scope or empty_scope()
        # TS: Math.min(params.pageSize ?? 10, 50) —— schema 已限 50，这里双保险
        page_size = min(page_size, 50)
        from_ = (page - 1) * page_size

        c = es_service.client
        if c is None:
            logger.warning("跳过搜索查询（ES 不可用）")
            return {"items": [], "total": 0, "page": page, "pageSize": page_size}

        # 刻意差异（TS searchDocuments 无此行）：先确保索引存在再搜。ensure_index 幂等，
        # 代价仅一次 HEAD/GET；好处是首个空库上搜索不会因 index_not_found 被 catch 吞成
        # 「搜索查询失败」告警。行为等价（都返回空分页），无副作用。
        await self.ensure_es_index()

        filters: list[dict[str, Any]] = []
        # 可见性子句放 filter 上下文（只筛不参与打分，避免权限条件扰动 BM25 排序）。
        # 子句内部是 bool.should（公开 / 本人 / 团队 三选一），与下方分类 / 作者过滤是 AND 关系。
        visibility = es_visibility_filter(scope, DOC_VISIBILITY_FIELDS)
        if visibility is not None:
            filters.append(visibility)
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
                    # 可见性字段一并回传（对齐 TS `teamId: src.teamId ?? null` /
                    # `isPublic: src.isPublic ?? null`）：前端据此展示「公开 / 团队 / 私有」
                    # 标签，也是排查「为什么这条我能搜到」的依据
                    "teamId": src.get("teamId"),
                    "isPublic": src.get("isPublic"),
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
