"""全文检索接口（对应 TS search/search.controller.ts）

- POST /api/v1/search：关键词检索已发布文档（ES kh_document），返回命中词高亮
- POST /api/v1/search/hybrid：chunk 级混合检索（ES kh_chunk 关键词 + PG doc_chunk 向量，RRF 融合）
均需 `search` 权限。
"""
from fastapi import Depends
from typing import Annotated

from app.api.auth_route import auth_router
from app.api.deps import get_current_user
from app.api.permission_guard import require_permission
from app.common.document_access import access_from_user
from app.schemas.auth import AuthUser
from app.schemas.search import (
    HybridChunkItem,
    HybridSearchRequest,
    HybridSearchResponse,
    SearchRequest,
    SearchResponse,
)
from app.services.retrieval import hybrid_retrieval_service
from app.services.search_index import search_index_service

router = auth_router()

# 当前用户依赖（可见性过滤用；对应 TS @CurrentUser() user: AuthUser）
_USER = Annotated[AuthUser, Depends(get_current_user)]


@router.post("", dependencies=[Depends(require_permission("search"))])
async def search_documents(dto: SearchRequest, user: _USER) -> SearchResponse:
    """关键词全文检索（带命中词高亮）

    传当前用户计算可见性范围（公开 ∪ 本人 ∪ 所在团队；管理员/审核员不受限）——
    命中列表会回传标题与摘要，须与 chunk 级检索同口径过滤。
    """
    result = await search_index_service.search_documents(
        keyword=dto.keyword,
        page=dto.page,
        page_size=dto.pageSize,
        category_id=dto.categoryId,
        author_id=dto.authorId,
        scope=access_from_user(user),
    )
    return SearchResponse(**result)


@router.post(
    "/hybrid",
    dependencies=[Depends(require_permission("search"))],
)
async def hybrid_search_documents(
    dto: HybridSearchRequest, user: _USER
) -> HybridSearchResponse:
    """chunk 级混合检索：ES 关键词 + PG 向量 → RRF 融合 → Reranker 精排

    传当前用户计算可见性范围（公开 ∪ 本人 ∪ 所在团队；管理员/审核员不受限）。
    """
    items = await hybrid_retrieval_service.retrieve(
        query=dto.query, top_k=dto.topK, user=user
    )
    return HybridSearchResponse(items=[HybridChunkItem(**item) for item in items])
