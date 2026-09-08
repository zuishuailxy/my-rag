"""
RAG 对话服务（对应 TS AiChatService：混合检索 → LLM 作答 → 溯源）

链路：kh_chunk 混合检索（关键词 + 向量 + RRF + rerank）→ DeepSeek 生成回答 →
抽取回答中的 [n] 编号，返回实际引用的资料摘录（ChatSource，可追溯）。

大模型统一用 DeepSeek（项目 LLM 约定，与 kg_extraction 一致）：
- baseUrl：OPENAI_BASE_URL → LLM_BASE_URL（默认 https://api.deepseek.com/v1）
- model：MODEL_NAME → LLM_MODEL（默认 deepseek-v4-flash，即 DeepSeek-V4-Flash）
- API Key fallback 链：OPENAI_API_KEY → LLM_API_KEY → DASHSCOPE_API_KEY

deepseek-v4-flash 默认 thinking 模式，与 kg_extraction 一致关闭以得到直接回答。
"""
import json
import logging
import re
from typing import Any

from fastapi import HTTPException
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.pipeline.types import ChunkHit
from app.schemas.auth import AuthUser
from app.services.chat_session import chat_session_service
from app.services.retrieval import hybrid_retrieval_service

logger = logging.getLogger("AiChatService")

# 溯源摘录长度（对齐 TS EXCERPT_LEN）
_EXCERPT_LEN = 200
# 上下文里每条资料正文截断（对齐 TS slice(0, 800)）
_CONTEXT_SNIPPET_LEN = 800
# 匹配回答中引用编号 [n]（对齐 TS CITATION_RE）
_CITATION_RE = re.compile(r"\[(\d+)\]")
_TEMPERATURE = 0.2

_SYSTEM_PROMPT = (
    "你是企业知识库助手。只根据「检索到的资料」回答用户问题。"
    "若资料不足以回答，明确说不知道，不要编造。"
    "凡是依据某条资料作出的陈述，必须在句末标注对应编号，如 [1]、[2]。"
    "编号必须与资料列表一致，不要标注未使用的编号，不要编造文档标题或链接。"
    "回答简洁，必要时列出条目。"
)


class AiChatService:
    """RAG 对话：混合检索召回 → DeepSeek 作答 → 抽取溯源"""

    def __init__(self) -> None:
        # 未配置 API Key 时保持 None，chat() 里抛 503（对齐 TS 构造函数早退 + 503）
        self._llm: ChatOpenAI | None = _build_llm()

    async def chat(
        self,
        db: AsyncSession,
        content: str,
        top_k: int = 5,
        user: AuthUser | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """对用户问题作答，返回 { sessionId, answer, sources }

        有 user 时问答落库（append_turn）：无 sessionId 新建会话，
        标题仍为默认名时用首问覆盖（对齐 TS chat）。
        """
        trimmed = content.strip()
        if not trimmed:
            # 空问题不落库，sessionId 原样透传（对齐 TS 早退分支）
            return {"sessionId": session_id, "answer": "请输入问题。", "sources": []}

        hits = await hybrid_retrieval_service.retrieve(trimmed, top_k)
        if not hits:
            logger.log(logging.INFO, "RAG 对话无召回：queryLength=%d，返回无内容提示", len(trimmed))
            answer = "知识库里没有相关内容。"
            sess = (
                await chat_session_service.append_turn(db, user.user_id, session_id, trimmed, answer, [])
                if user
                else None
            )
            return {
                "sessionId": str(sess.id) if sess else session_id,
                "answer": answer,
                "sources": [],
            }

        if self._llm is None:
            logger.error(
                "未配置 LLM API Key，无法生成回答：keys(OPENAI/LLM/DASHSCOPE) 均缺失"
            )
            raise HTTPException(
                status_code=503,
                detail="未配置 OPENAI_API_KEY / LLM_API_KEY / DASHSCOPE_API_KEY，无法生成回答",
            )

        hit_objects = [ChunkHit.from_row(hit) for hit in hits]
        context = self.build_context(hit_objects)
        response = await self._llm.ainvoke(
            [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=f"检索到的资料：\n{context}\n\n用户问题：{trimmed}"),
            ]
        )

        answer = (
            response.content
            if isinstance(response.content, str)
            else json.dumps(response.content, ensure_ascii=False)
        )
        sources = self.to_cited_sources(answer, hit_objects)
        logger.info(
            "RAG 对话完成：hits=%d, cited=%d, answerLength=%d",
            len(hit_objects),
            len(sources),
            len(answer),
        )
        # 问答落库（对齐 TS：user 存在时 append_turn，返回会话 id）
        sess = (
            await chat_session_service.append_turn(
                db, user.user_id, session_id, trimmed, answer, sources
            )
            if user
            else None
        )
        return {
            "sessionId": str(sess.id) if sess else session_id,
            "answer": answer,
            "sources": sources,
        }

    # ---- 溯源 ----

    def to_cited_sources(self, answer: str, hits: list[ChunkHit]) -> list[dict]:
        """从回答中抽出 [n]，只返回实际引用的资料；未标注时回退为全部召回（摘录）"""
        cited: set[int] = set()
        for match in _CITATION_RE.finditer(answer):
            n = int(match.group(1))
            if 1 <= n <= len(hits):
                cited.add(n)

        indexes = sorted(cited) if cited else list(range(1, len(hits) + 1))
        return [self.to_source(index, hits[index - 1]) for index in indexes]

    def to_source(self, index: int, hit: ChunkHit) -> dict:
        return {
            "index": index,
            "documentId": hit.documentId,
            "documentTitle": hit.documentTitle,
            "heading": hit.heading,
            "excerpt": self.excerpt(hit.content),
            "score": hit.score,
        }

    @staticmethod
    def excerpt(content: str) -> str:
        """摘要：压缩空白 + 超长截断（对齐 TS excerpt）"""
        text = re.sub(r"\s+", " ", content).strip()
        if len(text) <= _EXCERPT_LEN:
            return text
        return f"{text[:_EXCERPT_LEN]}..."

    @staticmethod
    def build_context(hits: list[ChunkHit]) -> str:
        """把召回块拼成 LLM 上下文（编号 [n] + 标题 + 章节 + 正文截断）"""
        parts = []
        for i, src in enumerate(hits, start=1):
            heading = f" / {src.heading}" if src.heading else ""
            snippet = (
                src.content[:_CONTEXT_SNIPPET_LEN] + "..."
                if len(src.content) > _CONTEXT_SNIPPET_LEN
                else src.content
            )
            parts.append(f"[{i}] {src.documentTitle}{heading}\n{snippet}")
        return "\n\n".join(parts)


def _build_llm() -> ChatOpenAI | None:
    """构建 DeepSeek 对话 LLM；未配置 API Key 返回 None"""
    api_key = (
        settings.OPENAI_API_KEY or settings.LLM_API_KEY or settings.DASHSCOPE_API_KEY
    )
    if not api_key:
        return None

    base_url = settings.OPENAI_BASE_URL or settings.LLM_BASE_URL
    model = settings.MODEL_NAME or settings.LLM_MODEL
    timeout_ms = settings.AI_CHAT_TIMEOUT_MS
    timeout_s = timeout_ms / 1000 if timeout_ms > 0 else 60.0

    return ChatOpenAI(
        api_key=SecretStr(api_key),
        model=model,
        temperature=_TEMPERATURE,
        timeout=timeout_s,
        max_retries=0,
        # DeepSeek 走 Chat Completions 兼容层，不要切 OpenAI Responses API
        use_responses_api=False,
        base_url=base_url,
        # deepseek-v4-flash 默认 thinking 模式，关闭以得到直接可溯源回答（对齐 kg_extraction）
        extra_body={"thinking": {"type": "disabled"}},
    )


# 全局单例
ai_chat_service = AiChatService()