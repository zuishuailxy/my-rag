"""
RAG 检索 / 对话 / 会话 DTO 与响应（对应 TS dto/rag-search.dto.ts + dto/chat.dto.ts
+ dto/session.dto.ts）

- rag/search：只检索不生成回答（返回 kh_chunk 命中详情，schema 复用 search.HybridChunkItem 语义）
- ai/chat：混合检索后由 LLM（DeepSeek）作答，返回 answer + 可追溯 sources + sessionId
- ai/sessions：会话 CRUD（分页 / 新建 / 改名 / 删除 / 消息列表）
"""

from datetime import datetime
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
    # 可选会话 ID：缺省则新建会话（对应 TS dto.sessionId）
    sessionId: str | None = Field(default=None, description="会话 ID，缺省新建")


class ChatSource(BaseModel):
    """返给前端的溯源条目（摘录，不含整块正文；对应 TS ChatSource）"""

    index: int = Field(..., description="资料编号，与回答中的 [n] 对应")
    documentId: str = ""
    documentTitle: str = ""
    heading: str | None = None
    excerpt: str = ""
    score: float | None = None


class ChatResponse(BaseModel):
    """RAG 对话响应（对应 TS AiChatService.chat 返回 { sessionId, answer, sources }）"""

    sessionId: str | None = None
    answer: str = ""
    sources: List[ChatSource] = Field(default_factory=list)


# ---- 会话（对应 TS dto/session.dto.ts + 实体序列化）----


class CreateSessionRequest(BaseModel):
    """新建会话请求（对应 TS CreateSessionDto；title 缺省时用默认标题「新对话」）"""

    title: str | None = Field(default=None, max_length=80, description="会话标题")


class QuerySessionRequest(BaseModel):
    """会话分页查询（对应 TS QuerySessionDto；service 默认 page=1 / pageSize=20）

    刻意差异：page / pageSize 补了上下界（page ≥1，pageSize 1~100），
    TS 未贴 source 未确认校验，按项目分页 DTO 惯例收紧。
    """

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    pageSize: int = Field(default=20, ge=1, le=100, description="每页条数")


class UpdateSessionRequest(BaseModel):
    """会话改名请求（对应 TS UpdateSessionDto；rename 直接 trim + 截断 80）"""

    title: str = Field(..., min_length=1, description="新标题（必填）")


class SessionResponse(BaseModel):
    """会话响应（对应 TS AiSessionEntity 序列化；bigint 全部转字符串）"""

    id: str
    userId: str
    title: str
    createdAt: datetime
    updatedAt: datetime


class SessionPageResponse(BaseModel):
    """会话分页响应（对应 TS { items, total, page, pageSize }）"""

    items: List[SessionResponse] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    pageSize: int = 20


class MessageResponse(BaseModel):
    """会话消息响应（对应 TS AiMessageEntity 序列化；bigint 转字符串）"""

    id: str
    sessionId: str
    role: str = Field(..., description="user / assistant")
    content: str
    sources: List[ChatSource] | None = None
    createdAt: datetime


class SessionDeleteResponse(BaseModel):
    """删除会话响应（对应 TS remove 返回 { message: '已删除' }）"""

    message: str = "已删除"