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

# 单条文档「长度告警」阈值 —— 只告警，**不截断**。
#
# 历史：这里曾是 _MAX_DOC_CHARS = 2000 的硬截断（从 TS 版对齐而来，未经验证）。
# 实测 467 条 chunk，最长 rerank 输入 1093 字符，**该阈值一次都没触发过**；
# 而 DashScope qwen3.7-text-rerank 的单条上限是 30,000 token（≈6 万字符），
# 2000 只有真实上限的 1/30 —— 它既没防住什么，也没优化什么。
#
# 为什么不截：chunk 长度应由 RAG_CHUNK_SIZE 保证，而不是在送 API 时二次静默裁剪。
# 静默裁剪有两个坏处：① 丢掉 chunk 尾部（实测 LLM 侧截 800 时 4.1% 的评测答案
# 关键词落在截断区）；② 掩盖 chunk 配置失控 —— 该报的错被切成「看起来正常」。
#
# 超限时 API 会直接返回 HTTP 400（官方明确「不会截断输入内容」），
# 这里提前打 WARN 让它可观测，而不是等 rerank 静默降级成 RRF 排序。
_WARN_DOC_CHARS = 8000


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
        oversized = [len(d) for d in documents if len(d) > _WARN_DOC_CHARS]
        if oversized:
            logger.warning(
                "rerank 输入异常长（未截断，交 API 判定）：%d/%d 条超 %d 字符，最长 %d。"
                "通常意味着 RAG_CHUNK_SIZE 被调大，请确认单条未超模型上限（3 万 token）",
                len(oversized),
                len(documents),
                _WARN_DOC_CHARS,
                max(oversized),
            )
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
        """候选块 → 送入 rerank 的纯文本（标题 + 章节标题 + 正文全文）。

        不截断：长度由 RAG_CHUNK_SIZE 控制，见 _WARN_DOC_CHARS 的说明。
        """
        heading = f"{hit.heading}\n" if hit.heading else ""
        return f"{hit.documentTitle}\n{heading}{hit.content}".strip()


# 全局单例
reranker_service = RerankerService()
