"""
chunk 级混合检索召回（方案 B：关键词 ES kh_chunk + 语义 PG doc_chunk，RRF 融合）

对应 TS VectorIndexService.searchHybrid（向量 + 关键词 → RRF 粗融合）：
- 关键词通道：ES kh_chunk multi_match（document_title^2 + content，IK ik_smart）top-N
- 语义通道：doc_chunk.embedding（PG，HNSW 余弦）top-N
- 两路并行发起（asyncio.gather，对齐 Promise.all）
- RRF：score = Σ 1/(C + rank)，C 默认 60，按 chunk_id 跨两路去重合并；
  每条附各通道原始分（bm25Score / vectorScore，对齐 ChunkHit）

返回每条带 PG 全量行（content / heading / document_title 等）。
任一路不可用 / 为空时单路降级不抛错；query 向量化失败只走关键词（对齐 TS embedQuery 容错，
真正的 embed 调用通常在编排层 HybridRetrievalService 完成并注入 query_vector）。

文档级全文检索（ES kh_document）见 services/search_index.py；
rerank 精排在上层 services/retrieval.py（HybridRetrievalService）。
可见性：本层按 scope 过滤两条召回通道（关键词走 ES filter、向量走 JOIN kh_document），
scope 由上层 retrieval.retrieve 依 AuthUser 计算（access_from_user）。
"""
import asyncio
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.common.document_access import (
    CHUNK_VISIBILITY_FIELDS,
    DocumentAccessScope,
    es_visibility_filter,
    wrap_es_query,
)
from app.core.config import settings
from app.db.es import es_service
from app.db.session import async_session_factory
from app.models.document_status import DocumentStatus
from app.services.embedding import embedding_provider
from app.services.es_index import CHUNK_INDEX

logger = logging.getLogger("HybridSearchService")

# 结果字段（对齐 doc_chunk 列，camelCase 输出在 schema 层转换）
_FIELDS = (
    "chunk_id, document_id, chunk_index, content, heading, document_title, "
    "total_chunks, category_id, author_id, team_id, doc_status, publish_time"
)

# 哨兵：区分「调用方未传 query_vector（需内部 embed）」与「显式传 None（跳过向量，仅关键词）」
_UNSET: Any = object()


async def _vector_recall(
    query_vec: list[float],
    limit: int,
    scope: DocumentAccessScope | None = None,
) -> list[tuple[str, float]]:
    """PG doc_chunk 余弦召回 → [(chunk_id, cosine_similarity)]（<=> 为余弦距离）

    可见性：本项目向量在 PG（TS 在 ES），故这里 JOIN kh_document 按可见性过滤
    （对齐 TS canReadDocument 的判定：作者本人 ∪ 已发布且(公开 ∪ 所在团队)）。
    用 JOIN 而非在 doc_chunk 冗余 is_public，好处是文档改权限立即生效、无需同步。
    """
    where_sql, vis_params = _visibility_sql(scope)
    async with async_session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT c.chunk_id, 1 - (c.embedding <=> :qv) AS sim "
                "FROM doc_chunk c JOIN kh_document d ON d.id = c.document_id "
                f"WHERE {where_sql} "
                "ORDER BY c.embedding <=> :qv LIMIT :limit"
            ),
            {"qv": str(query_vec), "limit": limit, **vis_params},
        )
    return [(r[0], float(r[1])) for r in rows.all() if r[0] and r[1] is not None]


def _visibility_sql(scope: DocumentAccessScope | None) -> tuple[str, dict[str, Any]]:
    """可见性 WHERE 子句 + 绑定参数（PG 向量通道用）

    scope 为 None 或 unrestricted → 不加限制（对齐 TS searchHybrid：scope 不传则不过滤）。
    否则：作者本人放行；其余要求 status=1(已发布) 且 (公开 或 团队命中)。
    deleted=false 恒加（兜底：软删文档的残留块不应被召回）。
    """
    if scope is None or scope.unrestricted:
        return "d.deleted = false", {}

    params: dict[str, Any] = {}
    author_sql = "FALSE"
    uid = _to_int(scope.user_id)
    if uid is not None:
        params["vis_uid"] = uid
        author_sql = "d.author_id = :vis_uid"

    team_sql = "FALSE"
    team_ints = [int(t) for t in scope.team_ids if _to_int(t) is not None]
    if team_ints:
        params["vis_teams"] = team_ints
        # 显式 CAST 让 asyncpg 按 bigint[] 编码，避免 bigint = text 的类型不匹配
        team_sql = "d.team_id = ANY(CAST(:vis_teams AS bigint[]))"

    params["vis_status"] = int(DocumentStatus.PUBLISHED)
    return (
        "d.deleted = false AND ("
        f"{author_sql} OR (d.status = :vis_status AND (d.is_public = true OR {team_sql}))"
        ")",
        params,
    )


def _to_int(value: str | None) -> int | None:
    """字符串 → int；空串 / 非法返回 None（不可用于 SQL 比较）"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _keyword_recall(
    query: str,
    limit: int,
    scope: DocumentAccessScope | None = None,
) -> list[tuple[str, float]]:
    """ES kh_chunk 关键词召回 → [(chunk_id, _score)]；ES 不可用返回 []

    标题加权 document_title^2 + content，IK ik_smart（对齐 vector-index.keywordSearch）。
    可见性：相关性查询放 must、可见性 filter 放 filter（wrapEsQuery），避免影响打分。
    """
    if not es_service.is_available or es_service.client is None:
        logger.warning("ES 不可用，关键词通道跳过（仅向量召回）")
        return []
    vis = (
        es_visibility_filter(scope, CHUNK_VISIBILITY_FIELDS) if scope else None
    )
    try:
        resp = await es_service.client.search(
            index=CHUNK_INDEX,
            size=limit,
            query=wrap_es_query(
                {
                    "multi_match": {
                        "query": query,
                        "fields": ["document_title^2", "content"],
                        "analyzer": "ik_smart",
                    }
                },
                vis,
            ),
            source={"includes": ["chunk_id"]},
        )
    except Exception as err:
        logger.warning("ES 关键词召回失败（跳过该路）：%s", err)
        return []
    results: list[tuple[str, float]] = []
    for hit in resp["hits"]["hits"]:
        cid = hit["_source"].get("chunk_id")
        if not cid:
            continue
        results.append((cid, float(hit.get("_score") or 0)))
    return results


async def hybrid_search(
    query: str,
    top_k: int | None = None,
    vector_candidates: int | None = None,
    keyword_candidates: int | None = None,
    query_vector: list[float] | None = _UNSET,
    rrf_c: int | None = None,
    scope: DocumentAccessScope | None = None,
) -> list[dict]:
    """关键词（ES）+ 语义（PG）并行召回，RRF 融合后返回 chunk 详情列表（对齐 searchHybrid）

    - top_k / vector_candidates / keyword_candidates / rrf_c 缺省取自配置
      RAG_HYBRID_TOP_K / RAG_RRF_C；
    - query_vector 不传（哨兵）时内部尝试 embed，失败仅走关键词；显式传 None 直接跳过
      向量通道（如编排层已 embed 失败降级）；
    - scope 为可见性范围（两条通道都按它过滤）。**不传 = 不过滤**，与 TS searchHybrid
      的 `scope?: DocumentAccessScope` 语义一致；生产链路请走 services/retrieval.py，
      那里会用 access_from_user / empty_scope 保证始终有 scope。
    每条结果附 bm25Score / vectorScore（各通道原始分），对齐 ChunkHit。
    """
    query = query.strip()
    if not query:
        return []

    hybrid_top_k = _clamp(top_k)
    vector_candidates = _clamp(vector_candidates)
    keyword_candidates = _clamp(keyword_candidates)
    rrf_k = rrf_c if rrf_c and rrf_c > 0 else settings.RAG_RRF_C

    if query_vector is _UNSET:
        query_vec = await _embed_query(query)
    else:
        query_vec = query_vector

    vector_tasks: list[Any] = []
    if query_vec:
        vector_tasks.append(_vector_recall(query_vec, vector_candidates, scope))
    vector_tasks.append(_keyword_recall(query, keyword_candidates, scope))
    results_per_channel = await asyncio.gather(*vector_tasks)
    vector_results = results_per_channel[0] if query_vec else []
    keyword_results = results_per_channel[-1]

    # RRF 融合（按 chunk_id 跨两路叠加；附各通道原始分）
    fused: dict[str, dict[str, float]] = {}

    def _add(results: list[tuple[str, float]], keyword: bool) -> None:
        for rank, (cid, score) in enumerate(
            sorted(results, key=lambda x: x[1], reverse=True), start=1
        ):
            entry = fused.setdefault(cid, {"rrf": 0.0, "bm25": 0.0, "vector": 0.0})
            entry["rrf"] += 1.0 / (rrf_k + rank)
            if keyword:
                entry["bm25"] = score
            else:
                entry["vector"] = score

    _add(vector_results, keyword=False)
    _add(keyword_results, keyword=True)

    merged = sorted(fused.items(), key=lambda kv: -kv[1]["rrf"])[:hybrid_top_k]
    if not merged:
        return []

    # 取 PG 全量行（保序）
    ids = [cid for cid, _ in merged]
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text(f"SELECT {_FIELDS} FROM doc_chunk WHERE chunk_id = ANY(:ids)"),
                {"ids": ids},
            )
        ).mappings().all()
    by_id = {r["chunk_id"]: dict(r) for r in rows}

    results: list[dict] = []
    for cid, channels in merged:
        row = by_id.get(cid)
        if row is None:
            continue
        results.append(_to_camel_row(row, channels))
    return results


async def _embed_query(query: str) -> list[float] | None:
    """query 向量化；失败仅走关键词（对齐 TS HybridRetrievalService.embedQuery 容错）"""
    try:
        return (await embedding_provider.embed_texts([query]))[0]
    except Exception as err:  # noqa: BLE001
        message = str(err)
        logger.warning("查询向量化失败，仅走关键词：%s", message)
        return None


def _clamp(top_k: int | None) -> int:
    """缺省取配置 RAG_HYBRID_TOP_K；不在 [1, 50] 则钳制"""
    value = top_k if top_k is not None else settings.RAG_HYBRID_TOP_K
    return max(1, min(value, 50))


def _to_camel_row(row: dict, channels: dict[str, float]) -> dict:
    """PG 行（snake_case + datetime/bigint）→ camelCase dict（schema 输出用）

    bigint 字段（documentId/categoryId/authorId/teamId）转字符串，
    与 document_to_dict 同约定（对应 TS bigintTransformer，防 JS Number 精度丢失）。
    """
    out: dict = {
        "score": round(channels["rrf"], 6),
        "bm25Score": round(channels["bm25"], 6),
        "vectorScore": round(channels["vector"], 6),
    }
    for src, dst in (
        ("chunk_id", "chunkId"),
        ("document_id", "documentId"),
        ("chunk_index", "chunkIndex"),
        ("content", "content"),
        ("heading", "heading"),
        ("document_title", "documentTitle"),
        ("total_chunks", "totalChunks"),
        ("category_id", "categoryId"),
        ("author_id", "authorId"),
        ("team_id", "teamId"),
        ("doc_status", "docStatus"),
        ("publish_time", "publishTime"),
    ):
        value = row.get(src)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif src in ("document_id", "category_id", "author_id", "team_id") and value is not None:
            value = str(value)
        out[dst] = value
    return out