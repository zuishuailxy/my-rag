"""
RAG 检索 / 对话 / 会话 DTO 与响应

- rag/search：只检索不生成回答（返回 kh_chunk 命中详情，schema 复用 search.HybridChunkItem 语义）
- ai/chat：混合检索后由 LLM（DeepSeek）作答，返回 answer + 可追溯 sources + sessionId
- ai/sessions：会话 CRUD（分页 / 新建 / 改名 / 删除 / 消息列表）
"""

from datetime import datetime
from typing import Any, List

from pydantic import BaseModel, Field


class RagSearchRequest(BaseModel):
    """RAG 混合检索请求"""

    query: str = Field(..., min_length=1, description="检索关键词")
    topK: int = Field(default=5, ge=1, le=20, description="精排后返回条数，默认 5")


class ChatRequest(BaseModel):
    """RAG 对话请求"""

    content: str = Field(..., min_length=1, description="用户问题")
    topK: int = Field(default=5, ge=1, le=10, description="召回块数，默认 5")
    # 可选会话 ID：缺省则新建会话
    sessionId: str | None = Field(default=None, description="会话 ID，缺省新建")


class ChatSource(BaseModel):
    """返给前端的溯源条目"""

    index: int = Field(..., description="资料编号，与回答中的 [n] 对应")
    documentId: str = ""
    documentTitle: str = ""
    heading: str | None = None
    excerpt: str = ""
    score: float | None = None


class ChatResponse(BaseModel):
    """RAG 对话响应"""

    sessionId: str | None = None
    answer: str = ""
    sources: List[ChatSource] = Field(default_factory=list)


# ---- 会话----


class CreateSessionRequest(BaseModel):
    """新建会话请求"""

    title: str | None = Field(default=None, max_length=80, description="会话标题")


class QuerySessionRequest(BaseModel):
    """会话分页查询

        page / pageSize 补了上下界（page ≥1，pageSize 1~100），
    ，按项目分页 DTO 惯例收紧。
    """

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    pageSize: int = Field(default=20, ge=1, le=100, description="每页条数")


class UpdateSessionRequest(BaseModel):
    """会话改名请求"""

    title: str = Field(..., min_length=1, description="新标题（必填）")


class SessionResponse(BaseModel):
    """会话响应"""

    id: str
    userId: str
    title: str
    createdAt: datetime
    updatedAt: datetime


class SessionPageResponse(BaseModel):
    """会话分页响应"""

    items: List[SessionResponse] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    pageSize: int = 20


class MessageResponse(BaseModel):
    """会话消息响应"""

    id: str
    sessionId: str
    role: str = Field(..., description="user / assistant")
    content: str
    sources: List[ChatSource] | None = None
    createdAt: datetime


class SessionDeleteResponse(BaseModel):
    """删除会话响应"""

    message: str = "已删除"


# ---- 流式对话----


class ChatStreamMessagePart(BaseModel):
    """消息片段"""

    type: str | None = None
    text: str | None = None


class ChatStreamMessage(BaseModel):
    """UIMessage"""

    role: str | None = None
    parts: List[ChatStreamMessagePart] = Field(default_factory=list)


class ChatStreamRequest(BaseModel):
    """流式对话请求"""

    messages: List[ChatStreamMessage] = Field(..., description="useChat 消息数组")
    sessionId: str | None = Field(default=None, description="会话 ID，缺省新建")
    topK: int = Field(default=5, ge=1, le=10, description="召回块数，默认 5")
    id: str | None = Field(default=None, description="useChat 消息 id，透传不使用")
    trigger: Any | None = Field(default=None, description="useChat 触发器，放行")
    messageId: Any | None = Field(default=None, description="useChat 消息 id，放行")
