"""
chunk 级混合检索（方案 B：关键词 ES kh_chunk + 语义 PG doc_chunk，RRF 融合）

对齐 ES 侧 vector-index.service 的混合检索语义：
- 关键词通道：ES kh_chunk multi_match（document_title^2 + content，IK ik_smart）top-N
- 语义通道：doc_chunk.embedding（PG，HNSW 余弦）top-N
- 两路并行发起（asyncio.gather，对齐 Promise.all）
- RRF：score = Σ 1/(k + rank)，k=60，按 chunk_id 跨两路去重合并；
  每条附各通道原始分（bm25Score / vectorScore，对齐 ChunkHit）

返回每条带 PG 全量行（content / heading / document_title 等）。
ES 不可用只走向量、向量为空只走关键词，任一路故障降级不抛错。
文档级全文检索（ES kh_document）见 services/search_index.py；Reranker / 权限过滤待后续。
"""
import asyncio
import logging
from datetime import datetime

from sqlalchemy import text

from app.db.es import es_service
from app.db.session import async_session_factory
from app.services.embedding import embedding_provider
from app.services.es_index import CHUNK_INDEX

logger = logging.getLogger("HybridSearchService")

# RRF 常数（与 ES RRF 默认一致）
RRF_K = 60

# 结果字段（对齐 doc_chunk 列，camelCase 输出在 schema 层转换）
_FIELDS = (
    "chunk_id, document_id, chunk_index, content, heading, document_title, "
    "total_chunks, category_id, author_id, team_id, doc_status, publish_time"
)


async def _vector_recall(query_vec: list[float], limit: int) -> list[tuple[str, float]]:
    """PG doc_chunk 余弦召回 → [(chunk_id, cosine_similarity)]（<=> 为余弦距离）"""
    async with async_session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT chunk_id, 1 - (embedding <=> :qv) AS sim "
                "FROM doc_chunk ORDER BY embedding <=> :qv LIMIT :limit"
            ),
            {"qv": str(query_vec), "limit": limit},
        )
    return [(r[0], float(r[1])) for r in rows.all() if r[0] and r[1] is not None]


async def _keyword_recall(query: str, limit: int) -> list[tuple[str, float]]:
    """ES kh_chunk 关键词召回 → [(chunk_id, _score)]；ES 不可用返回 []

    标题加权 document_title^2 + content，IK ik_smart（对齐 vector-index.keywordSearch）。
    """
    if not es_service.is_available or es_service.client is None:
        logger.warning("ES 不可用，关键词通道跳过（仅向量召回）")
        return []
    try:
        resp = await es_service.client.search(
            index=CHUNK_INDEX,
            size=limit,
            query={
                "multi_match": {
                    "query": query,
                    "fields": ["document_title^2", "content"],
                    "analyzer": "ik_smart",
                }
            },
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
    top_k: int = 10,
    vector_candidates: int = 20,
    keyword_candidates: int = 20,
) -> list[dict]:
    """关键词（ES）+ 语义（PG）并行混合检索，RRF 融合后返回 chunk 详情列表

    每条结果附加 bm25Score / vectorScore（各通道原始分），对齐 ES 的 ChunkHit。
    """
    query = query.strip()
    if not query:
        return []
    top_k = max(1, min(top_k, 50))

    query_vec = (await embedding_provider.embed_texts([query]))[0]
    vector_results, keyword_results = await asyncio.gather(
        _vector_recall(query_vec, vector_candidates),
        _keyword_recall(query, keyword_candidates),
    )

    # RRF 融合（按 chunk_id 跨两路叠加；附各通道原始分）
    fused: dict[str, dict[str, float]] = {}

    def _add(results: list[tuple[str, float]], keyword: bool) -> None:
        for rank, (cid, score) in enumerate(
            sorted(results, key=lambda x: x[1], reverse=True), start=1
        ):
            entry = fused.setdefault(cid, {"rrf": 0.0, "bm25": 0.0, "vector": 0.0})
            entry["rrf"] += 1.0 / (RRF_K + rank)
            if keyword:
                entry["bm25"] = score
            else:
                entry["vector"] = score

    _add(vector_results, keyword=False)
    _add(keyword_results, keyword=True)

    merged = sorted(fused.items(), key=lambda kv: -kv[1]["rrf"])[:top_k]
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