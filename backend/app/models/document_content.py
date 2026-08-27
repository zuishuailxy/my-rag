"""
文档正文模型（对应 Mongoose DocumentContent / MongoDB document_content）

与 PostgreSQL kh_document 一对一：_id ↔ content_id，documentId ↔ id
"""
from datetime import datetime

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field


class DocumentContent(BaseModel):
    """文档正文（MongoDB document_content 集合）"""

    model_config = ConfigDict(
        from_attributes=True,
        arbitrary_types_allowed=True,
    )

    # ObjectId，对应 kh_document.content_id（仿 Mongoose 客户端生成 _id）
    id: ObjectId = Field(default_factory=ObjectId, serialization_alias="_id")

    # 关联的文档元数据 ID（kh_document.id）
    document_id: str = Field(serialization_alias="documentId")

    # Markdown 正文
    content: str = Field(default="")

    # 正文字符数
    content_length: int = Field(default=0, serialization_alias="contentLength")

    # 正文摘要 / 预览
    content_summary: str = Field(default="", serialization_alias="contentSummary")

    # 版本号
    version: int = Field(default=1)

    # 逻辑删除
    deleted: bool = Field(default=False)

    # 创建时间（对应 Mongoose timestamps: true）
    created_at: datetime | None = Field(default=None, serialization_alias="createdAt")

    # 更新时间（对应 Mongoose timestamps: true）
    updated_at: datetime | None = Field(default=None, serialization_alias="updatedAt")
