"""图谱检索响应 schema（对应 TS graph/graph.controller.ts 返回结构）

图谱节点 / 边 / 关键词检索的命中项。字段名对齐 TS（camelCase）。
"""
from pydantic import BaseModel


class GraphNodeItem(BaseModel):
    """知识实体节点（TS listNodes 返回）"""

    id: str | None = None
    name: str | None = None
    type: str | None = None
    description: str | None = None


class GraphEdgeItem(BaseModel):
    """实体间 RELATED_TO 边（TS listEdges 返回）"""

    source: str | None = None
    target: str | None = None
    relation: str | None = None
    weight: float | None = None


class GraphSearchItem(BaseModel):
    """关键词检索命中（TS searchGraph 返回）

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
