"""知识图谱检索接口（对应 TS graph/graph.controller.ts）

GET /api/v1/graph/search：关键词检索实体/文档/块节点
GET /api/v1/graph/nodes：知识实体节点列表
GET /api/v1/graph/edges：实体间 RELATED_TO 边列表

TS 的 GraphController 未加任何守卫装饰器，对齐为「仅登录」（auth_router 默认全拦）。
"""
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.auth_route import auth_router
from app.schemas.graph import GraphEdgeItem, GraphNodeItem, GraphSearchItem
from app.services.graph_build import graph_build_service

router = auth_router()


@router.get("/search")
async def search_graph(
    keyword: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[GraphSearchItem]:
    """图谱关键词检索（实体名/描述、文档标题/摘要、块标题/正文）"""
    rows = await graph_build_service.search_graph(keyword, limit)
    return [GraphSearchItem(**row) for row in rows]


@router.get("/nodes")
async def list_nodes(
    type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[GraphNodeItem]:
    """知识实体节点列表（可按 type 过滤）"""
    rows = await graph_build_service.list_nodes(type, limit)
    return [GraphNodeItem(**row) for row in rows]


@router.get("/edges")
async def list_edges(
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> list[GraphEdgeItem]:
    """实体间 RELATED_TO 边列表"""
    rows = await graph_build_service.list_edges(limit)
    return [GraphEdgeItem(**row) for row in rows]
