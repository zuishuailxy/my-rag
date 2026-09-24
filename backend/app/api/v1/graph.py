"""知识图谱检索接口

GET /api/v1/graph/overview：全景图（文档/实体/标签 + 统计，供前端力导向图）
GET /api/v1/graph/search：关键词检索实体/文档/块节点
GET /api/v1/graph/nodes：知识实体节点列表
GET /api/v1/graph/edges：实体间 RELATED_TO 边列表)：全部端点挂
require_permission("search")。USER 角色种子里已有
search 权限码（400000000000000003），普通用户不受影响。

可见性（)）：四个端点都注入当前用户并换算成
DocumentAccessScope，交由 services/graph_build.py 在各条 Cypher 里按
「公开 ∪ 本人 ∪ 所在团队」过滤文档节点；ADMIN / REVIEWER 不受限。

⚠️ neo4j_access_params(scope=None) 的语义是「不过滤」，故此处四个端点
**必须**显式传 access_from_user(user)，漏传即等于放开全部图谱可见性。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.auth_route import auth_router
from app.api.deps import get_current_user
from app.api.permission_guard import require_permission
from app.common.document_access import access_from_user
from app.schemas.auth import AuthUser
from app.schemas.graph import (
    GraphEdgeItem,
    GraphNodeItem,
    GraphOverviewResponse,
    GraphSearchItem,
)
from app.services.graph_build import graph_build_service

router = auth_router()

# 当前用户依赖（可见性过滤用；) user: AuthUser）
_USER = Annotated[AuthUser, Depends(get_current_user)]


@router.get("/overview", dependencies=[Depends(require_permission("search"))])
async def graph_overview(
    user: _USER,
    keyword: str | None = None,
    entity_type: Annotated[str | None, Query(alias="entityType")] = None,
    from_time: Annotated[str | None, Query(alias="from")] = None,
    to_time: Annotated[str | None, Query(alias="to")] = None,
    doc_limit: Annotated[int | None, Query(alias="docLimit", ge=1, le=80)] = None,
) -> GraphOverviewResponse:
    """全景：文档 / 实体 / 标签 + 统计，供前端力导向图（按当前用户可见范围统计）"""
    result = await graph_build_service.get_overview(
        keyword=keyword,
        entity_type=entity_type,
        from_time=from_time,
        to_time=to_time,
        doc_limit=doc_limit,
        scope=access_from_user(user),
    )
    return GraphOverviewResponse(**result)


@router.get("/search", dependencies=[Depends(require_permission("search"))])
async def search_graph(
    user: _USER,
    keyword: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[GraphSearchItem]:
    """图谱关键词检索（实体名/描述、文档标题/摘要、块标题/正文）"""
    rows = await graph_build_service.search_graph(
        keyword, limit, access_from_user(user)
    )
    return [GraphSearchItem(**row) for row in rows]


@router.get("/nodes", dependencies=[Depends(require_permission("search"))])
async def list_nodes(
    user: _USER,
    type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[GraphNodeItem]:
    """知识实体节点列表（可按 type 过滤）"""
    rows = await graph_build_service.list_nodes(type, limit, access_from_user(user))
    return [GraphNodeItem(**row) for row in rows]


@router.get("/edges", dependencies=[Depends(require_permission("search"))])
async def list_edges(
    user: _USER,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> list[GraphEdgeItem]:
    """实体间 RELATED_TO 边列表"""
    rows = await graph_build_service.list_edges(limit, access_from_user(user))
    return [GraphEdgeItem(**row) for row in rows]
