"""
RAG 检索 / 对话 DTO 与响应（对应 TS dto/rag-search.dto.ts + dto/chat.dto.ts）

- rag/search：只检索不生成回答（返回 kh_chunk 命中详情，schema 复用 search.HybridChunkItem 语义）
- ai/chat：混合检索后由 LLM（DeepSeek）作答，返回 answer + 可追溯 sources
"""

from typing import List

from pydantic import BaseModel, Field


class RagSearchRequest(BaseModel):
    """RAG 混合检索请求（不生成回答；对应 TS RagSearchDto）"""

    query: str = Field(..., min_length=1, description="检索关键词")
    topK: int = Field(default=5, ge=1, le=20, description="精排后返回条数，默认 5")


class ChatRequest(BaseModel):
    """RAG 对话请求（对应 TS ChatDto）"""

    content: str = Field(..., min_length=1, description="用户问题")
    topK: int = Field(default=5, ge=1, le=10, description="召回块数，默认 5")


class ChatSource(BaseModel):
    """返给前端的溯源条目（摘录，不含整块正文；对应 TS ChatSource）"""

    index: int = Field(..., description="资料编号，与回答中的 [n] 对应")
    documentId: str = ""
    documentTitle: str = ""
    heading: str | None = None
    excerpt: str = ""
    score: float | None = None


class ChatResponse(BaseModel):
    """RAG 对话响应（对应 TS AiChatService.chat 返回 { answer, sources }）"""

    answer: str = ""
    sources: List[ChatSource] = Field(default_factory=list)