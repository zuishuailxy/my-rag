"""
文档元数据模型（对应 TypeORM DocumentEntity / PostgreSQL kh_document）
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.document_status import DocumentStatus

__all__ = ["DocumentEntity", "DocumentStatus"]


class DocumentEntity(Base):
    """文档元数据（PostgreSQL kh_document）"""

    __tablename__ = "kh_document"

    # 雪花 ID（应用层生成，非自增）
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # 标题
    title: Mapped[str] = mapped_column(String)

    # MongoDB document_content._id
    content_id: Mapped[str] = mapped_column(String, unique=True)

    # 摘要
    summary: Mapped[str | None] = mapped_column(String)

    # 分类 ID
    category_id: Mapped[int | None] = mapped_column(BigInteger)

    # 团队 ID
    team_id: Mapped[int | None] = mapped_column(BigInteger)

    # 作者 ID
    author_id: Mapped[int | None] = mapped_column(BigInteger)

    # 封面图 URL
    cover_image: Mapped[str | None] = mapped_column(String)

    # 标签（逗号分隔）
    tags: Mapped[str | None] = mapped_column(String)

    # 状态：0 草稿 / 1 已发布 / 2 已归档 / 3 待审核（见 document_status.py）
    status: Mapped[DocumentStatus] = mapped_column(
        SmallInteger, default=DocumentStatus.DRAFT, server_default="0"
    )

    # 备注
    remark: Mapped[str | None] = mapped_column(String)

    # 浏览数
    view_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # 点赞数
    like_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # 评论数
    comment_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # 收藏数
    favourite_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # 字数
    word_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # 发布时间
    publish_time: Mapped[datetime | None] = mapped_column(DateTime)

    # 是否公开
    is_public: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # 更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # 创建人 ID
    create_by: Mapped[int | None] = mapped_column(BigInteger)

    # 更新人 ID
    update_by: Mapped[int | None] = mapped_column(BigInteger)

    # 逻辑删除
    deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
