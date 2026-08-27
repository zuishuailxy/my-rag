"""
文档相关 DTO 与响应模型（对应 TypeORM CreateDocumentDto / QueryDocumentDto 等）
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.models.document import DocumentStatus


class DocumentCreate(BaseModel):
    """创建文档请求体"""

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )

    title: str = Field(..., description="标题")
    content: str = Field(..., description="Markdown 正文")
    summary: str | None = Field(default=None, description="摘要")
    category_id: str | None = Field(default=None, description="分类 ID")
    team_id: str | None = Field(default=None, description="团队 ID")
    author_id: str | None = Field(default=None, description="作者 ID")
    cover_image: str | None = Field(default=None, description="封面图 URL")
    tags: str | None = Field(default=None, description="标签（逗号分隔）")
    status: DocumentStatus | None = Field(default=None, description="状态")
    remark: str | None = Field(default=None, description="备注")
    is_public: bool | None = Field(default=None, description="是否公开")
    create_by: str | None = Field(default=None, description="创建人 ID")


class DocumentQuery(BaseModel):
    """文档列表查询参数（对应 TypeORM QueryDocumentDto）"""

    # alias_generator: snake_case 字段 ↔ camelCase 别名（categoryId/pageSize）
    # populate_by_name: 允许用字段名（snake_case）构造，Pylance 可识别
    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
    )

    # 标题（模糊）
    title: str | None = Field(default=None, description="标题（模糊）")

    # 分类 ID
    category_id: str | None = Field(default=None, description="分类 ID")

    # 团队 ID
    team_id: str | None = Field(default=None, description="团队 ID")

    # 作者 ID
    author_id: str | None = Field(default=None, description="作者 ID")

    # 状态：0 草稿 / 1 已发布 / 2 已归档
    status: DocumentStatus | None = Field(default=None, description="状态")

    # 页码，从 1 开始
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")

    # 每页条数
    page_size: int = Field(default=20, ge=1, le=100, description="每页条数")


class DocumentUpdate(BaseModel):
    """更新文档（字段均可选，对应 TypeORM UpdateDocumentDto）

    等价于 NestJS 的 PartialType(OmitType(CreateDocumentDto, ['createBy']))：
    取 CreateDocumentDto 除 createBy 外的全部字段并改为可选，另加 updateBy。
    注意：新增字段时需与 DocumentCreate 保持同步。
    """

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )

    # 标题
    title: str | None = Field(default=None, description="标题")

    # Markdown 正文
    content: str | None = Field(default=None, description="Markdown 正文")

    # 摘要
    summary: str | None = Field(default=None, description="摘要")

    # 分类 ID
    category_id: str | None = Field(default=None, description="分类 ID")

    # 团队 ID
    team_id: str | None = Field(default=None, description="团队 ID")

    # 作者 ID
    author_id: str | None = Field(default=None, description="作者 ID")

    # 封面图 URL
    cover_image: str | None = Field(default=None, description="封面图 URL")

    # 标签（逗号分隔）
    tags: str | None = Field(default=None, description="标签（逗号分隔）")

    # 状态：0 草稿 / 1 已发布 / 2 已归档
    status: DocumentStatus | None = Field(default=None, description="状态")

    # 备注
    remark: str | None = Field(default=None, description="备注")

    # 是否公开
    is_public: bool | None = Field(default=None, description="是否公开")

    # 更新人 ID
    update_by: str | None = Field(default=None, description="更新人 ID")


class DocumentMetadata(BaseModel):
    """文档元数据响应（列表项 / 详情共用；bigint 转字符串，键名 camelCase）"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 雪花 ID
    id: str
    # 标题
    title: str
    # MongoDB document_content._id
    content_id: str
    # 摘要
    summary: str | None = None
    # 分类 ID
    category_id: str | None = None
    # 团队 ID
    team_id: str | None = None
    # 作者 ID
    author_id: str | None = None
    # 封面图 URL
    cover_image: str | None = None
    # 标签（逗号分隔）
    tags: str | None = None
    # 状态：0 草稿 / 1 已发布 / 2 已归档
    status: DocumentStatus
    # 备注
    remark: str | None = None
    # 浏览数
    view_count: int
    # 点赞数
    like_count: int
    # 评论数
    comment_count: int
    # 收藏数
    favourite_count: int
    # 字数
    word_count: int
    # 发布时间
    publish_time: datetime | None = None
    # 是否公开
    is_public: bool
    # 创建时间
    created_at: datetime
    # 更新时间
    updated_at: datetime
    # 创建人 ID
    create_by: str | None = None
    # 更新人 ID
    update_by: str | None = None
    # 逻辑删除
    deleted: bool


class DocumentResponse(DocumentMetadata):
    """文档详情 / 创建 / 更新响应（元数据 + Markdown 正文）"""

    # Markdown 正文
    content: str | None = None


class DocumentListResponse(BaseModel):
    """文档列表分页响应"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 当前页数据（仅元数据，不含正文）
    items: list[DocumentMetadata]
    # 总条数
    total: int
    # 页码
    page: int
    # 每页条数
    page_size: int


class DocumentRemoveResponse(BaseModel):
    """软删除响应"""

    # 文档 ID
    id: str
    # 是否已删除
    deleted: bool
