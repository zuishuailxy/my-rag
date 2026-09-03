"""全文搜索请求 / 响应（对应 TS search/dto/search.dto.ts + SearchController 返回）

字段名保持 camelCase，与 ES kh_document 文档（SearchIndexService 写入）一致。
"""

from typing import Any, List

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    """全文搜索请求（对应 TS SearchDocumentsDto）

    keyword 必填；page / pageSize 可选且有上下界；categoryId / authorId 为可选过滤。
    """

    keyword: str = Field(..., description="搜索关键词（必填）")
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    pageSize: int = Field(default=10, ge=1, le=50, description="每页条数，1~50")
    categoryId: str | None = Field(default=None, description="按分类过滤")
    authorId: str | None = Field(default=None, description="按作者过滤")


class SearchHitItem(BaseModel):
    """单条命中（ES kh_document 文档原文 + 关键词高亮片段，对齐 TS searchDocuments 返回）

    仅保留 TS 实际返回的字段：id / title / summary / categoryId / tags / authorId /
    status / publishTime / score / highlight。列表不回传 content 全文（_source 已排除）。
    """

    id: str | None = None
    title: str | None = None
    summary: str | None = None
    categoryId: str | None = None
    tags: list[str] | None = None
    authorId: str | None = None
    status: int | None = None
    publishTime: str | None = None
    # 相关性得分（hit._score）；TS 用 `hit._score ?? 0`
    score: float | None = None
    # 关键词高亮片段（ES 默认 <em> 标签包裹命中词）；未命中的字段返回空列表
    highlight: dict[str, list[str]] | None = Field(default=None)


class SearchResponse(BaseModel):
    """全文搜索分页响应（对齐 TS { items, total, page, pageSize }）"""

    items: List[SearchHitItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    pageSize: int = 10
