"""图谱检索响应 schema

图谱节点 / 边 / 关键词检索的命中项，以及全景图（overview）聚合结构。
字段名）。
"""
from typing import Literal

from pydantic import BaseModel


class GraphNodeItem(BaseModel):
    """知识实体节点"""

    id: str | None = None
    name: str | None = None
    type: str | None = None
    description: str | None = None


class GraphEdgeItem(BaseModel):
    """实体间 RELATED_TO 边"""

    source: str | None = None
    target: str | None = None
    relation: str | None = None
    weight: float | None = None


class GraphSearchItem(BaseModel):
    """关键词检索命中

    可命中实体 / 文档 / 块节点，label 区分节点类型，snippet 为正文前 160 字。
    """

    id: str | None = None
    name: str | None = None
    label: str | None = None
    type: str | None = None
    title: str | None = None
    description: str | None = None
    heading: str | None = None
    documentId: str | None = None
    summary: str | None = None
    snippet: str | None = None


# ---- 全景图----

GraphOverviewKind = Literal["document", "entity", "tag"]
GraphOverviewEdgeKind = Literal["mentions", "related", "tagged"]


class GraphOverviewNode(BaseModel):
    """全景图节点：文档 / 实体 / 标签"""

    id: str
    name: str
    kind: GraphOverviewKind
    type: str | None = None
    documentId: str | None = None
    updatedAt: str | None = None
    description: str | None = None


class GraphOverviewEdge(BaseModel):
    """全景图边：文档→实体「提及」/ 实体→实体 RELATED_TO / 文档→标签「标注」"""

    source: str
    target: str
    relation: str
    kind: GraphOverviewEdgeKind


class GraphEntityTypeCount(BaseModel):
    """实体类型分组计数（stats.entityTypes 元素）"""

    type: str
    count: int


class GraphOverviewStats(BaseModel):
    """全景图统计"""

    nodeCount: int
    edgeCount: int
    documentCount: int
    entityCount: int
    tagCount: int
    mentionCount: int
    relatedCount: int
    entityTypes: list[GraphEntityTypeCount]


class GraphTopEntity(BaseModel):
    """被提及最多的实体（degree = MENTIONS 次数）"""

    name: str
    type: str | None = None
    degree: int


class GraphRecentNode(BaseModel):
    """最近更新的文档节点（不受筛选条件影响）"""

    id: str
    name: str
    kind: str
    updatedAt: str | None = None


class GraphOverviewResponse(BaseModel):
    """全景图聚合响应（供前端力导向图）"""

    nodes: list[GraphOverviewNode]
    edges: list[GraphOverviewEdge]
    stats: GraphOverviewStats
    topEntities: list[GraphTopEntity]
    recentNodes: list[GraphRecentNode]
    entityTypes: list[str]
