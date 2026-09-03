"""全文搜索接口（对应 TS search/search.controller.ts）

POST /api/v1/search：关键词检索已发布文档（ES kh_document），返回命中词高亮。
需 `search` 权限（对应 TS @RequirePermission(PermissionCode.search)）。
"""
from fastapi import Depends

from app.api.auth_route import auth_router
from app.api.permission_guard import require_permission
from app.schemas.search import SearchRequest, SearchResponse
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
