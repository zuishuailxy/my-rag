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
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.models.base import Base


class DocChunk(Base):
    """文档分片（含文本向量，支撑语义检索；对应 TS VectorIndexService 的 kh_chunk）"""

    __tablename__ = "doc_chunk"

    # 自增 ID
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # 所属文档（kh_document.id，级联删除）
    document_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("kh_document.id", ondelete="CASCADE"),
        index=True,
    )

    # 分片哈希（sha256(documentId:index)，唯一，upsert 主键）
    chunk_id: Mapped[str | None] = mapped_column(String(64), unique=True)

    # 分片序号（0 起）
    chunk_index: Mapped[int] = mapped_column(Integer)

    # 分片文本
    content: Mapped[str] = mapped_column(Text)

    # 所属章节标题（检索展示）
    heading: Mapped[str | None] = mapped_column(String(255))

    # 文档标题（检索展示）
    document_title: Mapped[str | None] = mapped_column(String(255))

    # 总块数
    total_chunks: Mapped[int | None] = mapped_column(Integer)

    # 冗余的文档属性（检索过滤用，与 kh_document 保持一致）
    category_id: Mapped[int | None] = mapped_column(BigInteger)
    author_id: Mapped[int | None] = mapped_column(BigInteger)
    team_id: Mapped[int | None] = mapped_column(BigInteger)
    doc_status: Mapped[int | None] = mapped_column(SmallInteger)
    publish_time: Mapped[datetime | None] = mapped_column(DateTime)

    # 文本向量（对齐 schema vector(1024)；对应 ES dense_vector）
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.EMBEDDING_DIMENSION))

    # 创建 / 更新时间（created_at 对应 ES indexed_at）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
