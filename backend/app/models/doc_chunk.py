"""
文档分片模型（PGVector doc_chunk 表）
"""
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.models.base import Base


class DocChunk(Base):
    """文档分片（含文本向量，支撑语义检索）"""

    __tablename__ = "doc_chunk"

    # 自增 ID
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # 所属文档（kh_document.id，级联删除）
    document_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("kh_document.id", ondelete="CASCADE"),
        index=True,
    )

    # 分片序号（0 起）
    chunk_index: Mapped[int] = mapped_column(Integer)

    # 分片文本
    content: Mapped[str] = mapped_column(Text)

    # 文本向量（对齐 schema vector(1024)）
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.EMBEDDING_DIM))

    # 创建 / 更新时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
