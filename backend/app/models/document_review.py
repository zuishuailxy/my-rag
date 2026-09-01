"""
文档审核记录模型（PostgreSQL kh_document_review）

一次「提交审核」一行；approve/reject 后 review_result 非空，不再出现在待办列表。
状态流转（见 docs/文档审核流程.jpeg）：
  Draft(0) → 提交审核 → PendingReview(3) → 审核通过 → Published(1) → 归档 → Archived(2)
                                    ↘ 审核驳回/返回修改 → Draft(0)

⚠️ schema 真相源是 init-scripts/postgresql/init.sql：改字段请先改那里的 DDL 再同步本模型，
应用启动的 create_all 只建不 ALTER（见 main.py lifespan 注释）。
"""

from datetime import datetime
from enum import IntEnum

from sqlalchemy import BigInteger, DateTime, Index, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReviewResult(IntEnum):
    """
    审核结果（对应 TS ReviewResult 枚举）

    注意：待审不是枚举值，而是 review_result IS NULL。
    """

    APPROVED = 1  # 通过
    REJECTED = 2  # 驳回


class DocumentReview(Base):
    """文档发布审核记录（kh_document_review）"""

    __tablename__ = "kh_document_review"

    # 雪花 ID
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # 被审文档 ID → kh_document.id
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 审核人 ID；待审时为 NULL
    reviewer_id: Mapped[int | None] = mapped_column(BigInteger)

    # 审核人姓名
    reviewer_name: Mapped[str | None] = mapped_column(String)

    # 审核结果：NULL=待审 / 1=通过 / 2=驳回
    review_result: Mapped[int | None] = mapped_column(SmallInteger)

    # 审核意见（驳回必填）
    review_comment: Mapped[str | None] = mapped_column(String)

    # 提审前文档 status（0 草稿 / 1 已发布）
    before_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    # 审核完成时间
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)

    # 提交审核时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # ---- 索引 ----

    __table_args__ = (
        Index("idx_kh_document_review_document_id", "document_id"),
        Index(
            "idx_kh_document_review_pending",
            "review_result",
            postgresql_where="review_result IS NULL",
        ),
    )
