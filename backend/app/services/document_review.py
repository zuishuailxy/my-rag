"""
文档发布审核服务（对应 TS DocumentReviewService）

与 kh_document_review 表对应：每次 submit 插入一条记录，approve / reject 回填结果。
开关：环境变量 DOCUMENT_REQUIRE_APPROVAL（默认 true；严格等于 'false' 时 publish 免审）。

生命周期：submitForReview 创建（review_result=NULL）→ approve / reject 结案。
同一 document_id 可有多条历史记录，但同时最多一条待审（应用层校验，无 DB 约束）。
"""

import logging
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.bigint import parse_bigint, require_bigint
from app.common.snowflake import next_snowflake_id
from app.core.config import settings
from app.db.mongo import get_document_content_collection
from app.models.document import DocumentEntity
from app.models.document_review import DocumentReview, ReviewResult
from app.models.document_status import DocumentStatus, can_submit_review
from app.schemas.document_review import (
    ReviewTaskItem,
    ReviewTaskListResponse,
    ReviewTaskQuery,
)
from app.services.pipeline_publisher import pipeline_publisher

logger = logging.getLogger("DocumentReviewService")


def _utcnow() -> datetime:
    """UTC 当前时间（naive，适配 TIMESTAMP WITHOUT TIME ZONE 列）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_status(value: int | DocumentStatus | None) -> DocumentStatus:
    """列值（int）→ DocumentStatus；未知值原样按 int 构造（IntEnum 允许）"""
    if isinstance(value, DocumentStatus):
        return value
    return DocumentStatus(int(value) if value is not None else 0)


class DocumentReviewService:
    """文档发布审核服务"""

    # ------------------------------------------------------------- 开关

    def is_require_approval(self) -> bool:
        """
        是否开启发布审核（默认 true）

        判定与 TS 逐字一致：`config.get('DOCUMENT_REQUIRE_APPROVAL', 'true') !== 'false'`，
        即只有严格等于字符串 "false" 才免审。
        """
        return settings.DOCUMENT_REQUIRE_APPROVAL != "false"

    # ------------------------------------------------------------- 提交审核

    async def submit_for_review(
        self, session: AsyncSession, document_id: str
    ) -> DocumentEntity:
        """
        提交审核：Draft / Published → PendingReview

        若来自 Published，先清索引（审核期间不可检索）。
        同一文档已有待审记录时抛 400。
        """
        doc = await self._find_document_or_throw(session, document_id)

        if not can_submit_review(_as_status(doc.status)):
            raise HTTPException(
                status_code=400, detail="只有草稿或已发布状态的文档才能提交审核"
            )

        pending = await self._find_pending_review(session, doc.id)
        if pending is not None:
            raise HTTPException(status_code=400, detail="该文档已有待审核任务")

        before_status = int(doc.status) if doc.status is not None else 0

        # 写入审核流水；review_result 留空表示待审
        review = DocumentReview(
            id=next_snowflake_id(),
            document_id=doc.id,
            before_status=before_status,
        )
        session.add(review)
        await session.flush()  # 对应 TS em.save(review)

        doc.status = DocumentStatus.PENDING_REVIEW
        await session.commit()
        await session.refresh(doc)

        if before_status == int(DocumentStatus.PUBLISHED):
            await self._safe_unpublish(document_id)

        logger.info(
            "文档已提交审核：documentId=%s, reviewId=%s, beforeStatus=%s",
            document_id,
            review.id,
            before_status,
        )
        return doc

    # ------------------------------------------------------------- 审核通过

    async def approve_review(
        self,
        session: AsyncSession,
        review_id: str,
        reviewer_id: str,
        reviewer_name: str,
        review_comment: str | None = None,
    ) -> DocumentEntity:
        """审核通过 → Published + 重建索引

        参数顺序与 TS approveReview(reviewId, reviewerId, reviewerName, reviewComment?) 一致。
        reviewerId / reviewerName 必填（TS 同款）：审核人由 controller 从 JWT 取
        （userId 与 `realName ?? username`），不在请求体传，因此调用方一定拿得到。
        """
        # 审核人 ID 先校验：非法值直接 400，不能静默丢弃（否则审核记录会缺审核人）
        reviewer_pk = require_bigint(reviewer_id, "reviewerId")

        review = await self._find_pending_review_or_throw(session, review_id)

        review.review_result = ReviewResult.APPROVED
        review.reviewer_id = reviewer_pk
        review.reviewer_name = reviewer_name
        review.review_comment = review_comment
        review.reviewed_at = _utcnow()
        await session.commit()
        await session.refresh(review)

        doc = await self._find_document_or_throw(session, str(review.document_id))
        doc.status = DocumentStatus.PUBLISHED
        doc.publish_time = _utcnow()
        await session.commit()
        await session.refresh(doc)

        content = await self._load_content(doc.content_id)
        await self._safe_publish(doc, content)

        logger.info("审核通过：reviewId=%s, documentId=%s", review_id, doc.id)
        return doc

    # ------------------------------------------------------------- 审核驳回

    async def reject_review(
        self,
        session: AsyncSession,
        review_id: str,
        review_comment: str | None,
        reviewer_id: str,
        reviewer_name: str,
    ) -> DocumentEntity:
        """审核驳回 → Draft（驳回意见必填且 trim 后非空）

        参数顺序与 TS rejectReview(reviewId, reviewComment, reviewerId, reviewerName) 一致；
        reviewerId / reviewerName 必填（TS 同款，来源同 approve_review）。
        注意：意见必填在**服务层**校验（400「驳回意见不能为空」），不在 DTO 层——
        TS 的 ReviewDecisionDto 也未标必填，两边行为保持一致。
        """
        if not review_comment or not review_comment.strip():
            raise HTTPException(status_code=400, detail="驳回意见不能为空")

        # 审核人 ID 先校验：非法值直接 400，不能静默丢弃（否则审核记录会缺审核人）
        reviewer_pk = require_bigint(reviewer_id, "reviewerId")

        review = await self._find_pending_review_or_throw(session, review_id)

        review.review_result = ReviewResult.REJECTED
        review.reviewer_id = reviewer_pk
        review.reviewer_name = reviewer_name
        review.review_comment = review_comment.strip()
        review.reviewed_at = _utcnow()
        await session.commit()
        await session.refresh(review)

        doc = await self._find_document_or_throw(session, str(review.document_id))
        doc.status = DocumentStatus.DRAFT
        await session.commit()
        await session.refresh(doc)

        logger.info("审核驳回：reviewId=%s, documentId=%s", review_id, doc.id)
        return doc

    # ------------------------------------------------------------- 查询

    async def list_tasks(
        self, session: AsyncSession, query: ReviewTaskQuery
    ) -> ReviewTaskListResponse:
        """待办 / 已通过 / 已驳回 列表（按创建时间倒序分页）"""
        page = query.page
        page_size = query.page_size
        conditions = self._status_conditions(query.status)

        stmt = (
            select(DocumentReview)
            .where(*conditions)
            .order_by(DocumentReview.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = list((await session.scalars(stmt)).all())

        total_stmt = (
            select(func.count()).select_from(DocumentReview).where(*conditions)
        )
        total = int(await session.scalar(total_stmt) or 0)

        return ReviewTaskListResponse(
            items=[self._to_item(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_pending_count(self, session: AsyncSession) -> int:
        """待审任务总数"""
        stmt = (
            select(func.count())
            .select_from(DocumentReview)
            .where(DocumentReview.review_result.is_(None))
        )
        return int(await session.scalar(stmt) or 0)

    async def get_current_review(
        self, session: AsyncSession, document_id: str
    ) -> ReviewTaskItem | None:
        """该文档当前待审任务（无则 None）"""
        pk = parse_bigint(document_id)
        if pk is None:
            return None
        stmt = (
            select(DocumentReview)
            .where(
                DocumentReview.document_id == pk,
                DocumentReview.review_result.is_(None),
            )
            .order_by(DocumentReview.created_at.desc())
            .limit(1)
        )
        review = await session.scalar(stmt)
        return self._to_item(review) if review is not None else None

    async def get_review_history(
        self, session: AsyncSession, document_id: str
    ) -> list[ReviewTaskItem]:
        """该文档全部审核记录（含已通过、已驳回），按创建时间倒序"""
        pk = parse_bigint(document_id)
        if pk is None:
            return []
        stmt = (
            select(DocumentReview)
            .where(DocumentReview.document_id == pk)
            .order_by(DocumentReview.created_at.desc())
        )
        return [self._to_item(row) for row in await session.scalars(stmt)]

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _status_conditions(status: str | None) -> list:
        """列表过滤条件：pending（默认）/ approved / rejected"""
        if status == "approved":
            return [DocumentReview.review_result == ReviewResult.APPROVED]
        if status == "rejected":
            return [DocumentReview.review_result == ReviewResult.REJECTED]
        return [DocumentReview.review_result.is_(None)]

    @staticmethod
    def _to_item(review: DocumentReview) -> ReviewTaskItem:
        """ORM 实体 → 响应 DTO（bigint 转字符串，键名 camelCase）"""
        # 按字段名构造；输出 camelCase 由 FastAPI response_model 按别名序列化
        return ReviewTaskItem(
            id=str(review.id),
            document_id=str(review.document_id),
            reviewer_id=str(review.reviewer_id) if review.reviewer_id else None,
            reviewer_name=review.reviewer_name,
            review_result=review.review_result,
            review_comment=review.review_comment,
            before_status=review.before_status,
            reviewed_at=review.reviewed_at,
            created_at=review.created_at,
        )

    async def _find_pending_review_or_throw(
        self, session: AsyncSession, review_id: str
    ) -> DocumentReview:
        """按 id 取待审记录；不存在 404，已结案 400"""
        review = await self._find_review(session, review_id)
        if review is None:
            raise HTTPException(
                status_code=404, detail=f"Review {review_id} not found"
            )
        if review.review_result is not None:
            raise HTTPException(status_code=400, detail="该审核任务已处理")
        return review

    async def _find_review(
        self, session: AsyncSession, review_id: str
    ) -> DocumentReview | None:
        """按 id 取审核记录（id 非法时返回 None）"""
        pk = parse_bigint(review_id)
        if pk is None:
            return None
        stmt = select(DocumentReview).where(DocumentReview.id == pk)
        return await session.scalar(stmt)

    async def _find_pending_review(
        self, session: AsyncSession, document_pk: int
    ) -> DocumentReview | None:
        """该文档当前待审记录（无则 None）"""
        stmt = (
            select(DocumentReview)
            .where(
                DocumentReview.document_id == document_pk,
                DocumentReview.review_result.is_(None),
            )
            .limit(1)
        )
        return await session.scalar(stmt)

    async def _find_document_or_throw(
        self, session: AsyncSession, doc_id: str | int
    ) -> DocumentEntity:
        """按 id 查询未软删文档，不存在（或 id 非法 / 超出 bigint 范围）则抛 404"""
        pk = parse_bigint(str(doc_id))
        if pk is None:
            raise HTTPException(
                status_code=404, detail=f"Document {doc_id} not found"
            )
        stmt = select(DocumentEntity).where(
            DocumentEntity.id == pk, DocumentEntity.deleted.is_(False)
        )
        doc = await session.scalar(stmt)
        if doc is None:
            raise HTTPException(
                status_code=404, detail=f"Document {doc_id} not found"
            )
        return doc

    async def _load_content(self, content_id: str) -> str:
        """按 content_id 拉取未删除的正文（缺失或无效 ObjectId 时返回空串）"""
        try:
            oid = ObjectId(content_id)
        except Exception:
            return ""
        doc = await get_document_content_collection().find_one(
            {"_id": oid, "deleted": False}
        )
        return doc.get("content", "") if doc else ""

    async def _safe_publish(self, doc: DocumentEntity, content: str) -> None:
        """审核通过后投递索引（失败只告警，不回滚审核结论）"""
        try:
            await pipeline_publisher.after_publish(doc, content)
        except Exception as err:
            logger.warning(
                "审核通过后索引投递失败：documentId=%s, error=%s", doc.id, err
            )

    async def _safe_unpublish(self, document_id: str | int) -> None:
        """提交审核后清理索引（失败只告警，不回滚提审）"""
        try:
            await pipeline_publisher.after_unpublish(document_id)
        except Exception as err:
            logger.warning(
                "提交审核后索引清理失败：documentId=%s, error=%s", document_id, err
            )


# 全局单例（无状态）
document_review_service = DocumentReviewService()
