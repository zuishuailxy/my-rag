"""RAG 检索 / 对话 / 会话接口（对应 TS AiController）

- POST /api/v1/rag/search：RAG 混合检索（BM25 + 向量 → RRF → rerank），不调 LLM
- POST /api/v1/ai/chat：RAG 对话（混合检索后再由 DeepSeek 作答，写入本人会话）
- GET|POST /api/v1/ai/sessions：会话分页 / 新建
- GET /api/v1/ai/sessions/{id}/messages：会话消息列表
- PATCH|DELETE /api/v1/ai/sessions/{id}：改名 / 删除
均需 `search` 权限（对齐 TS 类级 @RequirePermission(PermissionCode.search)）。
"""
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_route import auth_router
from app.api.deps import get_current_user
from app.api.permission_guard import require_permission
from app.db.session import get_db_session
from app.models.ai_session import AiMessage, AiSession
from app.schemas.ai import (
    ChatRequest,
    ChatResponse,
    ChatStreamRequest,
    CreateSessionRequest,
    MessageResponse,
    QuerySessionRequest,
    RagSearchRequest,
    SessionDeleteResponse,
    SessionPageResponse,
    SessionResponse,
    UpdateSessionRequest,
)
from app.schemas.auth import AuthUser
from app.schemas.search import HybridChunkItem
from app.services.ai_chat import ai_chat_service
from app.services.ai_stream import ai_stream_service
from app.services.chat_session import chat_session_service
from app.services.retrieval import hybrid_retrieval_service

# 两个独立前缀（对齐 TS：AiController.search 在 /rag，chat 在 /ai）
router = auth_router()
rag_router: APIRouter = auth_router()

_DB = Annotated[AsyncSession, Depends(get_db_session)]
_USER = Annotated[AuthUser, Depends(get_current_user)]


def _session_vo(s: AiSession) -> SessionResponse:
    """实体 → 响应 VO（bigint 转字符串，对齐 TS bigintTransformer 序列化口径）"""
    return SessionResponse(
        id=str(s.id),
        userId=str(s.user_id),
        title=s.title,
        createdAt=s.created_at,
        updatedAt=s.updated_at,
    )


def _message_vo(m: AiMessage) -> MessageResponse:
    """实体 → 响应 VO（bigint 转字符串；sources 由 JSONB 直接构造 ChatSource）"""
    # JSONB 列类型上声明为 dict|list|None，写入侧保证是 list[dict] | None，
    # 此处 isinstance 收窄后交由 pydantic 把 dict 逐项校验成 ChatSource
    raw_sources = cast("list[dict[str, Any]] | None", m.sources) if isinstance(
        m.sources, list
    ) else None
    return MessageResponse(
        id=str(m.id),
        sessionId=str(m.session_id),
        role=m.role,
        content=m.content,
        sources=raw_sources,
        createdAt=m.created_at,
    )


@rag_router.post(
    "/search",
    dependencies=[Depends(require_permission("search"))],
)
async def rag_search(dto: RagSearchRequest, user: _USER) -> list[HybridChunkItem]:
    """RAG 混合检索：只召回不生成回答，返回 kh_chunk 命中详情

    传当前用户计算可见性范围（对应 TS search(dto, @CurrentUser() user)）。
    """
    items = await hybrid_retrieval_service.retrieve(
        dto.query.strip(), top_k=dto.topK, user=user
    )
    return [HybridChunkItem(**item) for item in items]


@router.post(
    "/chat",
    dependencies=[Depends(require_permission("search"))],
)
async def ai_chat(dto: ChatRequest, user: _USER, db: _DB) -> ChatResponse:
    """RAG 对话：混合检索 + DeepSeek 作答，写入本人会话（无 sessionId 则新建）"""
    result = await ai_chat_service.chat(db, dto.content, top_k=dto.topK, user=user, session_id=dto.sessionId)
    return ChatResponse(**result)


@router.post(
    "/chat/stream",
    dependencies=[Depends(require_permission("search"))],
)
async def ai_chat_stream(dto: ChatStreamRequest, user: _USER, db: _DB) -> StreamingResponse:
    """流式对话：agent 流式作答，UI Message Stream（AI SDK v6 SSE 协议）

    响应头对齐 TS pipeUIMessageStreamToResponse（content-type 由
    media_type 提供，协议版本头前端 DefaultChatTransport 校验用）。
    """
    return StreamingResponse(
        ai_stream_service.stream_chat(db, dto, user),
        media_type="text/event-stream",
        headers={
            "x-vercel-ai-ui-message-stream": "v1",
            "Cache-Control": "no-cache",
        },
    )


# ---- 会话（字面量段 /sessions 注册在动态段 /{session_id} 之前）----


@router.get(
    "/sessions",
    dependencies=[Depends(require_permission("search"))],
)
async def list_sessions(
    user: _USER,
    db: _DB,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(
        default=20, ge=1, le=100, alias="pageSize", description="每页条数"
    ),
) -> SessionPageResponse:
    """本人会话分页，updated_at 倒序"""
    result = await chat_session_service.page_mine(
        db, user.user_id, QuerySessionRequest(page=page, pageSize=page_size)
    )
    return SessionPageResponse(
        items=[_session_vo(s) for s in result["items"]],
        total=result["total"],
        page=result["page"],
        pageSize=result["pageSize"],
    )


@router.post(
    "/sessions",
    status_code=201,
    dependencies=[Depends(require_permission("search"))],
)
async def create_session(dto: CreateSessionRequest, user: _USER, db: _DB) -> SessionResponse:
    """新建会话（标题缺省用「新对话」）"""
    entity = await chat_session_service.create(db, user.user_id, dto)
    return _session_vo(entity)


@router.get(
    "/sessions/{session_id}/messages",
    dependencies=[Depends(require_permission("search"))],
)
async def list_messages(session_id: str, user: _USER, db: _DB) -> list[MessageResponse]:
    """会话消息列表，created_at / id 升序；非本人会话 404"""
    rows = await chat_session_service.list_messages(db, user.user_id, session_id)
    return [_message_vo(m) for m in rows]


@router.patch(
    "/sessions/{session_id}",
    dependencies=[Depends(require_permission("search"))],
)
async def rename_session(
    session_id: str, dto: UpdateSessionRequest, user: _USER, db: _DB
) -> SessionResponse:
    """会话改名（trim + 截断 80 字）；非本人会话 404"""
    entity = await chat_session_service.rename(db, user.user_id, session_id, dto)
    return _session_vo(entity)


@router.delete(
    "/sessions/{session_id}",
    dependencies=[Depends(require_permission("search"))],
)
async def remove_session(session_id: str, user: _USER, db: _DB) -> SessionDeleteResponse:
    """删除会话及其全部消息（先删消息再删会话，对齐 TS）"""
    result = await chat_session_service.remove(db, user.user_id, session_id)
    return SessionDeleteResponse(**result)
