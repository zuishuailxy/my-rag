"""知识图谱检索接口（对应 TS graph/graph.controller.ts）

GET /api/v1/graph/overview：全景图（文档/实体/标签 + 统计，供前端力导向图）
GET /api/v1/graph/search：关键词检索实体/文档/块节点
GET /api/v1/graph/nodes：知识实体节点列表
GET /api/v1/graph/edges：实体间 RELATED_TO 边列表

对齐 TS 类级 @RequirePermission(PermissionCode.search)：全部端点挂
require_permission("search")（2026-09-07 TS 加类级权限码后同步收紧；
此前 TS 无装饰器、Python 对齐为「仅登录」）。USER 角色种子里已有
search 权限码（400000000000000003），普通用户不受影响。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.auth_route import auth_router
from app.api.permission_guard import require_permission
from app.schemas.graph import (
    GraphEdgeItem,
    GraphNodeItem,
    GraphOverviewResponse,
    GraphSearchItem,
)
from app.services.graph_build import graph_build_service

router = auth_router()


@router.get("/overview", dependencies=[Depends(require_permission("search"))])
async def graph_overview(
    keyword: str | None = None,
    entity_type: Annotated[str | None, Query(alias="entityType")] = None,
    from_time: Annotated[str | None, Query(alias="from")] = None,
    to_time: Annotated[str | None, Query(alias="to")] = None,
    doc_limit: Annotated[int | None, Query(alias="docLimit", ge=1, le=80)] = None,
) -> GraphOverviewResponse:
    """全景：文档 / 实体 / 标签 + 统计，供前端力导向图"""
    result = await graph_build_service.get_overview(
        keyword=keyword,
        entity_type=entity_type,
        from_time=from_time,
        to_time=to_time,
        doc_limit=doc_limit,
    )
    return GraphOverviewResponse(**result)


@router.get("/search", dependencies=[Depends(require_permission("search"))])
async def search_graph(
    keyword: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[GraphSearchItem]:
    """图谱关键词检索（实体名/描述、文档标题/摘要、块标题/正文）"""
    rows = await graph_build_service.search_graph(keyword, limit)
    return [GraphSearchItem(**row) for row in rows]


@router.get("/nodes", dependencies=[Depends(require_permission("search"))])
async def list_nodes(
    type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[GraphNodeItem]:
    """知识实体节点列表（可按 type 过滤）"""
    rows = await graph_build_service.list_nodes(type, limit)
    return [GraphNodeItem(**row) for row in rows]


@router.get("/edges", dependencies=[Depends(require_permission("search"))])
async def list_edges(
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> list[GraphEdgeItem]:
    """实体间 RELATED_TO 边列表"""
    rows = await graph_build_service.list_edges(limit)
    return [GraphEdgeItem(**row) for row in rows]
