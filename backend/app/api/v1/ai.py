"""RAG 检索 / 对话接口（对应 TS AiController）

- POST /api/v1/rag/search：RAG 混合检索（BM25 + 向量 → RRF → rerank），不调 LLM
- POST /api/v1/ai/chat：RAG 对话（混合检索后再由 DeepSeek 作答，返回 answer + sources）
均需 `search` 权限。
"""
from fastapi import APIRouter, Depends

from app.api.auth_route import auth_router
from app.api.permission_guard import require_permission
from app.schemas.ai import ChatRequest, ChatResponse, RagSearchRequest
from app.schemas.search import HybridChunkItem
from app.services.ai_chat import ai_chat_service
from app.services.retrieval import hybrid_retrieval_service

# 两个独立前缀（对齐 TS：AiController.search 在 /rag，chat 在 /ai）
router = auth_router()
rag_router: APIRouter = auth_router()


@rag_router.post(
    "/search",
    dependencies=[Depends(require_permission("search"))],
)
async def rag_search(dto: RagSearchRequest) -> list[HybridChunkItem]:
    """RAG 混合检索：只召回不生成回答，返回 kh_chunk 命中详情"""
    items = await hybrid_retrieval_service.retrieve(dto.query.strip(), top_k=dto.topK)
    return [HybridChunkItem(**item) for item in items]


@router.post(
    "/chat",
    dependencies=[Depends(require_permission("search"))],
)
async def ai_chat(dto: ChatRequest) -> ChatResponse:
    """RAG 对话：混合检索 + DeepSeek 作答，返回 answer 与可溯源 sources"""
    result = await ai_chat_service.chat(dto.content, top_k=dto.topK)
    return ChatResponse(**result)