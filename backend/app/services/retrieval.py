"""
chunk 级混合检索编排

链路：
  1. 查询向量化（失败降级为 None，仅走关键词）
  2. 粗召回：ES 关键词 + PG 向量 → RRF 融合（hybrid_search，候选数 = RAG_HYBRID_TOP_K）
  3. 精排：全部融合候选交给 Reranker（fused.length，非先截 topK）；
     失败（配额 / 网络 / 未配 Key）则降级为 RRF 顺序，并用向量余弦相似度兜底过滤
  4. 过滤：minScore > 0 时按 rerank relevance_score 下限过滤，再截 topK
返回 topK 条 chunk 详情（dict，camelCase，对齐 schema）。
"""
import logging

from app.common.document_access import access_from_user, empty_scope
from app.core.config import settings
from app.pipeline.types import ChunkHit
from app.schemas.auth import AuthUser
from app.services.reranker import reranker_service
from app.services.search import hybrid_search

logger = logging.getLogger("HybridRetrievalService")


class HybridRetrievalService:
    """kh_chunk 混合检索：向量召回 + 关键词召回 → RRF 粗融合 → reranker 精排"""

    def __init__(
        self,
        hybrid_top_k: int | None = None,
        rrf_c: int | None = None,
        min_score: float | None = None,
        min_vector_score: float | None = None,
    ) -> None:
        # 缺省由 search.hybrid_search / settings 读配置；此处可显式覆盖
        self._hybrid_top_k = hybrid_top_k
        self._rrf_c = rrf_c
        self._min_score = settings.RAG_MIN_SCORE if min_score is None else min_score
        self._min_vector_score = (
            settings.RAG_MIN_VECTOR_SCORE
            if min_vector_score is None
            else min_vector_score
        )

    async def retrieve(
        self, query: str, top_k: int = 5, user: AuthUser | None = None
    ) -> list[dict]:
        """对 query 执行混合检索，返回 topK 条按相关性排序的 chunk 详情

        user 用于计算可见性范围（)）：
        不传时退化为「只能看到公开内容」（empty_scope），不会变成不过滤。
        """
        query = query.strip()
        if not query:
            return []

        # 可见性范围：有登录用户按其角色/团队算，无用户则仅公开
        scope = access_from_user(user) if user is not None else empty_scope()

        fused = await hybrid_search(
            query,
            top_k=self._hybrid_top_k,
            rrf_c=self._rrf_c,
            scope=scope,
        )
        if not fused:
            logger.log(logging.INFO, "混合检索无结果：queryLength=%d", len(query))
            return []

        #），截断发生在过滤之后
        reranked = await reranker_service.rerank(
            query,
            [ChunkHit.from_row(r) for r in fused],
            len(fused),
        )
        if reranked:
            by_id = {r["chunkId"]: r for r in fused}
            # 再截 topK
            kept = (
                [h for h in reranked if h.score >= self._min_score]
                if self._min_score > 0
                else reranked
            )
            ordered: list[dict] = []
            for hit in kept[:top_k]:
                row = by_id.get(hit.chunkId)
                if row is None:
                    continue
                row["score"] = hit.score
                ordered.append(row)
            best = f"{reranked[0].score:.3f}" if reranked else "-"
            logger.log(
                logging.INFO,
                "检索过滤：rerank=%d, minScore=%s, best=%s, kept=%d, return=%d",
                len(reranked),
                self._min_score,
                best,
                len(kept),
                len(ordered),
            )
            return ordered

        # rerank 不可用：保持 RRF 顺序；若调用方要求过滤（minScore > 0），
        # 改用向量余弦相似度兜底 —— 否则阈值整体失效，无关问题会全部漏进来
        # （实测负例空召回率 1.00 → 0.00）。显式关过滤（minScore <= 0）时不介入。
        if self._min_score <= 0:
            return fused[:top_k]

        # ⚠️ 兜底門控本身依赖向量分：若**向量通道也挂了**（欠费 / 网络），
        # 所有 vectorScore 都是 0，门控会把候选**全部滤空** → 整体召回变成 0，
        # 比不兜底还差。此时只能退回 RRF 原序（没有数据就别做数据驱动的判断）。
        has_vector_data = any((r.get("vectorScore") or 0.0) > 0 for r in fused)
        if not has_vector_data:
            logger.warning(
                "向量通道无数据，跳过向量相似度兜底（否则会滤空全部候选）：fused=%d",
                len(fused),
            )
            return fused[:top_k]

        fallback = [
            r
            for r in fused
            if (r.get("vectorScore") or 0.0) >= self._min_vector_score
        ]
        logger.warning(
            "rerank 不可用，降级为向量相似度兜底：minVectorScore=%s, fused=%d, kept=%d",
            self._min_vector_score,
            len(fused),
            len(fallback),
        )
        return fallback[:top_k]


# 全局单例
hybrid_retrieval_service = HybridRetrievalService()
