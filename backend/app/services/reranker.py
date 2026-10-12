"""
文本重排服务

对 RRF 粗排后的候选块打相关性分并重排（rerank 阶段）。
未启用 / 未配置 Key / 调用失败时返回 None，由上层降级为 RRF 顺序。
"""

import logging
from dataclasses import replace

import httpx

from app.core.config import settings
from app.pipeline.types import ChunkHit

logger = logging.getLogger("RerankerService")

# 单挑文本送入 rerank 前的最大长度
_MAX_DOC_CHARS = 2000


class RerankerService:
    """文本 reranker（DashScope text-rerank 专用端点）"""

    def __init__(self) -> None:
        # (..., 'true') !== 'false'` 一致
        self.enabled = settings.RAG_RERANK_ENABLED != "false"
        # API Key fallback 链：RERANK_API_KEY → DASHSCOPE_API_KEY → OPENAI_API_KEY
        self.api_key = (
            settings.RERANK_API_KEY
            or settings.DASHSCOPE_API_KEY
            or settings.OPENAI_API_KEY
        )
        self.model = settings.RAG_RERANK_MODEL
        self.endpoint = settings.RERANK_ENDPOINT

    def is_enabled(self) -> bool:
        return self.enabled and bool(self.api_key)

    async def rerank(
        self,
        query: str,
        candidates: list[ChunkHit],
        top_n: int,
    ) -> list[ChunkHit] | None:
        """按 query 与文档相关性重排，取 topN；失败返回 None（上层降级 RRF）"""
        if not candidates:
            return []
        if not self.is_enabled():
            logger.warning("Reranker 未启用或未配置 Key，跳过重排")
            return None

        documents = [self._to_document(hit) for hit in candidates]
        payload = {
            "model": self.model,
            "input": {"query": query, "documents": documents},
            "parameters": {
                "return_documents": False,
                "top_n": min(top_n, len(candidates)),
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self.endpoint, headers=headers, json=payload)
                body = resp.json()
        except Exception as err:  # 网络 / 超时 / 非 JSON
            logger.warning("Rerank 异常，降级为 RRF：%s", err)
            return None

        if resp.status_code != 200:
            logger.warning(
                "Rerank 调用失败：status=%s, code=%s, message=%s",
                resp.status_code,
                body.get("code", ""),
                body.get("message", ""),
            )
            return None

        # 回退顶层 results（兼容分支）
        results = (body.get("output") or {}).get("results") or body.get("results") or []
        if not results:
            logger.warning("Rerank 返回空结果，降级为 RRF")
            return None

        reranked: list[ChunkHit] = []
        for item in results:
            index = item["index"]
            if index < 0 or index >= len(candidates):
                continue
            # 不原地改输入的 ChunkHit
            reranked.append(replace(candidates[index], score=item["relevance_score"]))

        logger.info(
            "Rerank 完成：model=%s, in=%d, out=%d",
            self.model,
            len(candidates),
            len(reranked),
        )
        return reranked

    @staticmethod
    def _to_document(hit: ChunkHit) -> str:
        """候选块 → 送入 rerank 的纯文本（标题 + 章节标题 + 正文，超长截断）"""
        heading = f"{hit.heading}\n" if hit.heading else ""
        text = f"{hit.documentTitle}\n{heading}{hit.content}".strip()
        if len(text) > _MAX_DOC_CHARS:
            text = f"{text[:_MAX_DOC_CHARS]}..."
        return text


# 全局单例
reranker_service = RerankerService()
