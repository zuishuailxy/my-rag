"""
chunk 级混合检索（方案 B：关键词 ES kh_chunk + 语义 PG doc_chunk，RRF 融合）

对应架构「关键词召回 + 向量召回，RRF 融合 + Reranker 重排」的 chunk 层实现：
- 关键词通道：ES kh_chunk content（IK 中文分词）top-N
- 语义通道：doc_chunk.embedding（PG，HNSW 余弦）top-N
- RRF：score = Σ 1/(k + rank)，k=60，跨两路去重合并

返回每条带 PG 全量行（content / heading / document_title 等）与融合分。
文档级全文检索（ES kh_document）见 services/search_index.py；Reranker / 权限过滤待后续。
"""
import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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


async def _vector_recall(query_vec: list[float], limit: int) -> list[str]:
    """PG doc_chunk 余弦召回 → chunk_id 列表"""
    async with async_session_factory() as session:
        rows = await session.execute(
            text(f"SELECT chunk_id FROM doc_chunk ORDER BY embedding <=> :qv LIMIT :limit"),
            {"qv": str(query_vec), "limit": limit},
        )
    return [r[0] for r in rows.all() if r[0]]


async def _keyword_recall(query: str, limit: int) -> list[str]:
    """ES kh_chunk 关键词召回（IK 分词）→ chunk_id 列表；ES 不可用返回 []"""
    if not es_service.is_available or es_service.client is None:
        logger.warning("ES 不可用，关键词通道跳过（仅向量召回）")
        return []
    try:
        resp = await es_service.client.search(
            index=CHUNK_INDEX,
            query={"match": {"content": query}},
            size=limit,
            source={"includes": ["chunk_id"]},
        )
    except Exception as err:
        logger.warning("ES 关键词召回失败（跳过该路）：%s", err)
        return []
    return [h["_source"]["chunk_id"] for h in resp["hits"]["hits"]]


async def hybrid_search(
    query: str,
    top_k: int = 10,
    vector_candidates: int = 20,
    keyword_candidates: int = 20,
) -> list[dict]:
    """关键词（ES）+ 语义（PG）混合检索，RRF 融合后返回 chunk 详情列表"""
    query = query.strip()
    if not query:
        return []

    query_vec = (await embedding_provider.embed_texts([query]))[0]
    vector_ids, keyword_ids = await _vector_recall(query_vec, vector_candidates), await _keyword_recall(query, keyword_candidates)

    # RRF 融合
    rrf: dict[str, float] = {}
    for rank, cid in enumerate(vector_ids, start=1):
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank)
    for rank, cid in enumerate(keyword_ids, start=1):
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank)
    merged = sorted(rrf.items(), key=lambda x: -x[1])[:top_k]
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
    for cid, score in merged:
        row = by_id.get(cid)
        if row is None:
            continue
        results.append(_to_camel_row(row, score))
    return results


def _to_camel_row(row: dict, score: float) -> dict:
    """PG 行（snake_case + datetime）→ camelCase dict（schema 输出用）"""
    out: dict = {"score": round(score, 6)}
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
        out[dst] = value
    return out
