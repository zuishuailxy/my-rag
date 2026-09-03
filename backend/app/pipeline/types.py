"""
管线共用类型定义（对应 TS src/pipeline/types/pipeline.types.ts）

这些结构在 MQ 消息体、分块结果之间流转。
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import TypedDict


@dataclass
class DocumentChunk:
    """一篇文档切出来的一块文本（RAG：附 embedding 写入 doc_chunk）"""

    # 稳定 ID：sha256(documentId:index) 前 64 位，重建时可覆盖
    chunk_id: str
    document_id: str
    document_title: str
    # 实际送去嵌入 / 抽取的文本（通常含章节标题前缀）
    content: str
    # 所属 Markdown 标题；无标题章节为 None
    heading: str | None
    # 从 0 开始的块序号
    chunk_index: int
    # 该文档总块数（切完后回填）
    total_chunks: int
    category_id: str | None = None
    author_id: str | None = None
    team_id: str | None = None
    doc_status: int | None = None
    publish_time: str | None = None
    # 向量；分块阶段为空，EmbeddingService 填充后写入
    embedding: list[float] | None = None


@dataclass
class PipelineDocument:
    """管线内部使用的「文档快照」（Postgres 元数据 + Mongo 正文）"""

    id: str
    title: str
    content: str
    status: int
    summary: str | None = None
    category_id: str | None = None
    author_id: str | None = None
    team_id: str | None = None
    tags: str | None = None
    is_public: bool | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    publish_time: datetime | str | None = None
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None


class SearchDocumentData(TypedDict, total=False):
    """ES kh_document 一条记录（对应 TS PipelineOrchestrator.toSearchIndexDoc）

    字段名保持 camelCase，与 TS 侧写入的数据同构（已有索引可平滑复用）。
    由编排器在消费侧按 documentId 拉库构建（content 取全文），写入时由
    SearchIndexService 补 indexedAt。
    """

    id: str
    title: str
    summary: str | None
    # 正文全文（消费侧拉库获得；不再截断 1000 字）
    content: str | None
    tags: str | None
    status: int
    isPublic: bool | None
    categoryId: str | None
    authorId: str | None
    # 计数
    viewCount: int | None
    likeCount: int | None
    commentCount: int | None
    # ISO-8601
    publishTime: str | None
    createdAt: str | None
    updatedAt: str | None


@dataclass
class ExtractedEntity:
    """图谱实体（如「张三」「入职流程」「知识库」）"""

    name: str
    # PERSON / ORGANIZATION / CONCEPT / DOCUMENT / PROCESS / PRODUCT 等，见 docs/kg-extraction-schema.md
    type: str
    description: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass
class ExtractedRelation:
    """实体间关系：source -[relation]-> target"""

    source: str
    target: str
    relation: str
    weight: float = 0.5


@dataclass
class ExtractionResult:
    """单个 chunk 的抽取结果（对应 TS ExtractionResult）"""

    chunk_id: str | None = None
    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)


def to_iso_date(value: datetime | str | None) -> str | None:
    """转 ISO-8601 字符串（对应 TS toIsoDate；无效值返回 None）"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return None
