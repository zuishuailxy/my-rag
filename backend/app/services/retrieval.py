"""
chunk 级混合检索编排（对应 TS HybridRetrievalService：embed → searchHybrid → rerank）

链路：
  1. 查询向量化（失败降级为 None，仅走关键词）
  2. 粗召回：ES 关键词 + PG 向量 → RRF 融合（hybrid_search，候选数 = RAG_HYBRID_TOP_K）
  3. 精排：rrf 粗排后的候选按 query 交给 Reranker，失败则保持 RRF 顺序
返回 topK 条 chunk 详情（dict，camelCase，对齐 schema）。
"""
import logging

from app.pipeline.types import ChunkHit
from app.services.reranker import reranker_service
from app.services.search import hybrid_search

logger = logging.getLogger("HybridRetrievalService")


class HybridRetrievalService:
    """kh_chunk 混合检索：向量召回 + 关键词召回 → RRF 粗融合 → reranker 精排"""

    def __init__(self, hybrid_top_k: int | None = None, rrf_c: int | None = None) -> None:
        # 缺省由 search.hybrid_search 读配置；此处可显式覆盖（对齐 TS ConfigService）
        self._hybrid_top_k = hybrid_top_k
        self._rrf_c = rrf_c

    async def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        """对 query 执行混合检索，返回 topK 条按相关性排序的 chunk 详情"""
        query = query.strip()
        if not query:
            return []

        fused = await hybrid_search(
            query,
            top_k=self._hybrid_top_k,
            rrf_c=self._rrf_c,
        )
        if not fused:
            logger.log(logging.INFO, "混合检索无结果：queryLength=%d", len(query))
            return []

        reranked = await reranker_service.rerank(
            query,
            [ChunkHit.from_row(r) for r in fused],
            top_k,
        )
        if reranked:
            by_id = {r["chunkId"]: r for r in fused}
            ordered: list[dict] = []
            for hit in reranked[:top_k]:
                row = by_id.get(hit.chunkId)
                if row is None:
                    continue
                row["score"] = hit.score
                ordered.append(row)
            return ordered

        return fused[:top_k]


# 全局单例
hybrid_retrieval_service = HybridRetrievalService()