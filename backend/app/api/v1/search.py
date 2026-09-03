"""全文检索接口（对应 TS search/search.controller.ts）

- POST /api/v1/search：关键词检索已发布文档（ES kh_document），返回命中词高亮
- POST /api/v1/search/hybrid：chunk 级混合检索（ES kh_chunk 关键词 + PG doc_chunk 向量，RRF 融合）
均需 `search` 权限。
"""
from fastapi import Depends

from app.api.auth_route import auth_router
from app.api.permission_guard import require_permission
from app.schemas.search import (
    HybridChunkItem,
    HybridSearchRequest,
    HybridSearchResponse,
    SearchRequest,
    SearchResponse,
)
from app.services.search import hybrid_search
from app.services.search_index import search_index_service

router = auth_router()


@router.post("", dependencies=[Depends(require_permission("search"))])
async def search_documents(dto: SearchRequest) -> SearchResponse:
    """关键词全文检索（带命中词高亮）"""
    result = await search_index_service.search_documents(
        keyword=dto.keyword,
        page=dto.page,
        page_size=dto.pageSize,
        category_id=dto.categoryId,
        author_id=dto.authorId,
    )
    return SearchResponse(**result)


@router.post(
    "/hybrid",
    dependencies=[Depends(require_permission("search"))],
)
async def hybrid_search_documents(dto: HybridSearchRequest) -> HybridSearchResponse:
    """chunk 级混合检索：ES 关键词（IK）+ PG 向量（语义），RRF 融合"""
    items = await hybrid_search(query=dto.query, top_k=dto.topK)
    return HybridSearchResponse(items=[HybridChunkItem(**item) for item in items])
