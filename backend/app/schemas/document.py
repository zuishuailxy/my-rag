"""
文档相关 DTO 与响应模型
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
    """文档列表查询参数"""

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

    # 状态：0 草稿 / 1 已发布 / 2 已归档 / 3 待审核
    status: DocumentStatus | None = Field(default=None, description="状态")

    # 页码，从 1 开始
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")

    # 每页条数
    page_size: int = Field(default=20, ge=1, le=100, description="每页条数")


class DocumentUpdate(BaseModel):
    """更新文档（字段均可选）：
    取 CreateDocumentDto 除 createBy 外的全部字段并改为可选，另加 updateBy。
    注意：新增字段时需与 DocumentCreate 保持同步。

    所有字段均可选，未出现的字段保持原值不变（发 {} 也合法）。
    json_schema_extra.example 用于覆盖 Swagger UI 自动生成的示例 —— 默认示例
    会把 categoryId 等填成字面量 "string"，直接点 Execute 会触发 400
    `Invalid categoryId: string`。
    """

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={
            "example": {
                "title": "文档标题",
                "summary": "一句话摘要",
                "categoryId": "1001",
                "tags": "技术,文档",
                "isPublic": True,
                "updateBy": "1001",
            }
        },
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

    # 状态：0 草稿 / 1 已发布 / 2 已归档 / 3 待审核
    status: DocumentStatus | None = Field(
        default=None,
        description="状态：0 草稿 / 1 已发布 / 2 已归档 / 3 待审核。"
        "PATCH 仅允许 1→0（下架为草稿），其余状态变更请走 "
        "publish / archive / save-draft / 审核接口",
    )

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
    # 状态：0 草稿 / 1 已发布 / 2 已归档 / 3 待审核
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


class UploadParseDto(BaseModel):
    """上传解析元数据"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 分类 ID
    category_id: str | None = None
    # 团队 ID
    team_id: str | None = None
    # 作者 ID
    author_id: str | None = None
    # 标签（逗号分隔）
    tags: str | None = None
    # 备注
    remark: str | None = None
    # 创建人 ID
    create_by: str | None = None
    # 是否公开
    is_public: bool | None = None


class UploadParseResult(BaseModel):
    """上传并解析响应"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 创建的草稿文档 ID
    document_id: str
    # 文档标题（去掉扩展名）
    title: str
    # 原文件在 RustFS 的 URL（未启用时 null）
    file_url: str | None = None
    # 原文件大小（字节）
    file_size: int
    # 文件扩展名（小写，不含点）
    file_extension: str
    # 解析后 Markdown 长度
    content_length: int
    # 解析内容预览（前 200 字符）
    content_preview: str
    # 文档状态（草稿）
    status: DocumentStatus


class IndexResult(BaseModel):
    """文档索引响应（RAG 消费者）"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 文档 ID
    document_id: str
    # 生成的分片数
    chunk_count: int
