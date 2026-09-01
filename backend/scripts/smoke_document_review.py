"""
文档审核服务集成冒烟测试（对应 TS DocumentReviewService）

前置条件：本地 docker 的 postgres（kh_document / kh_document_review 已由 init.sql 建好）
与 mongodb 运行中。RabbitMQ 未启动时投递会降级为 warn，不影响断言。

用法：
    cd backend && .venv/bin/python scripts/smoke_document_review.py

测试会真实写入数据，结束时物理清理，不留脏数据。
"""
import asyncio
import logging

from bson import ObjectId
from fastapi import HTTPException
from sqlalchemy import delete
from smoke_auth import SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME

from app.db.mongo import close_mongo_client, get_document_content_collection
from app.db.session import async_session_factory
from app.mq.rabbitmq import rabbitmq_service
from app.models.document import DocumentEntity, DocumentStatus
from app.models.document_review import DocumentReview, ReviewResult
from app.schemas.document import DocumentCreate
from app.schemas.document_review import ReviewTaskQuery
from app.services.document import DocumentService
from app.services.document_review import document_review_service as svc


async def main() -> None:
    # 日志对齐 NestJS 风格：[LEVEL] [ClassName] 中文消息：key=value
    # 脚本独立运行时 root logger 默认 WARNING，需显式开启 INFO 才能看到服务日志
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
    )
    # SQL 回显日志会淹没业务日志（engine 创建时 SQLAlchemy 会强制把该 logger 设为 INFO）
    logging.getLogger("sqlalchemy.engine").disabled = True
    logging.getLogger("sqlalchemy.engine.Engine").disabled = True

    # 真实连接 RabbitMQ，验证审核通过 / 提审清索引的投递链路（MQ 不可用时服务降级不失败）
    await rabbitmq_service.start()

    doc_id: str | None = None
    content_oid: ObjectId | None = None
    try:
        async with async_session_factory() as session:
            # 0) 开关默认开启
            print("0) is_require_approval =", svc.is_require_approval())
            assert svc.is_require_approval() is True

            # 1) 建草稿 → 提交审核 → PendingReview
            created = await DocumentService().create(
                session,
                DocumentCreate(
                    title="审核冒烟文档",
                    content="审核流程验证正文",
                    status=DocumentStatus.DRAFT,
                ),
            )
            doc_id = created["id"]
            content_oid = ObjectId(created["contentId"])

            doc = await svc.submit_for_review(session, doc_id)
            print(
                "1) submit(draft): status =",
                int(doc.status),
                "| beforeStatus 期望 0",
            )
            assert int(doc.status) == int(DocumentStatus.PENDING_REVIEW)

            current = await svc.get_current_review(session, doc_id)
            assert current is not None and current.review_result is None
            assert current.before_status == 0
            print("   待审记录: reviewId =", current.id, "| reviewResult =", current.review_result)

            # 2) 重复提审 → 400
            #    与 TS 一致：文档已是 PendingReview，先命中 canSubmitReview 校验
            #    （「该文档已有待审核任务」分支仅在状态为 Draft/Published 而流水未结案时出现）
            try:
                await svc.submit_for_review(session, doc_id)
                raise AssertionError("重复提审未被拦截")
            except HTTPException as e:
                assert e.status_code == 400, e.status_code
                print("2) 重复提审 → 400 OK:", e.detail)

            # 3) 驳回：空意见 → 400
            #    审核人必填（TS 同款），这里用种子 reviewer 账号
            review_id = current.id
            try:
                await svc.reject_review(
                    session, review_id, "   ", SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME
                )
                raise AssertionError("空驳回意见未被拦截")
            except HTTPException as e:
                assert e.status_code == 400, e.status_code
                print("3) 空驳回意见 → 400 OK:", e.detail)

            # 4) 驳回 → Draft
            doc = await svc.reject_review(
                session,
                review_id,
                "  内容需补充  ",
                reviewer_id="9001",
                reviewer_name="张三",
            )
            print("4) reject: status =", int(doc.status), "| 期望 0（Draft）")
            assert int(doc.status) == int(DocumentStatus.DRAFT)

            history = await svc.get_review_history(session, doc_id)
            assert history[0].review_result == int(ReviewResult.REJECTED)
            assert history[0].review_comment == "内容需补充"  # trim 生效
            assert history[0].reviewer_name == "张三"
            assert history[0].reviewed_at is not None
            print("   历史首条: result =", history[0].review_result, "| comment =", history[0].review_comment)

            # 5) 已结案记录再次审核 → 400
            try:
                await svc.approve_review(
                    session, review_id, SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME
                )
                raise AssertionError("重复处理未被拦截")
            except HTTPException as e:
                assert e.status_code == 400, e.status_code
                print("5) 已结案记录再审 → 400 OK:", e.detail)

            # 6) 再次提审（Draft→PendingReview）→ 审核通过（审核人必填，原样落库）
            await svc.submit_for_review(session, doc_id)
            current = await svc.get_current_review(session, doc_id)
            doc = await svc.approve_review(
                session, current.id, SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME
            )
            print(
                "6) approve: status =",
                int(doc.status),
                "| publishTime =", doc.publish_time,
            )
            assert int(doc.status) == int(DocumentStatus.PUBLISHED)
            assert doc.publish_time is not None

            history = await svc.get_review_history(session, doc_id)
            approved = history[0]
            assert approved.review_result == int(ReviewResult.APPROVED)
            assert approved.reviewer_name == SEED_REVIEWER_USERNAME
            assert approved.reviewer_id == SEED_REVIEWER_ID
            print("   通过记录: reviewerName =", approved.reviewer_name, "| reviewerId =", approved.reviewer_id)

            # 7) 已发布再提审 → 状态回 PendingReview（触发 safe_unpublish 清索引）
            doc = await svc.submit_for_review(session, doc_id)
            print("7) submit(published): status =", int(doc.status), "| beforeStatus 应为 1")
            assert int(doc.status) == int(DocumentStatus.PENDING_REVIEW)
            assert (await svc.get_current_review(session, doc_id)).before_status == 1

            # 8) 列表分页与过滤
            pending = await svc.list_tasks(session, ReviewTaskQuery(status="pending"))
            approved_list = await svc.list_tasks(session, ReviewTaskQuery(status="approved"))
            rejected_list = await svc.list_tasks(session, ReviewTaskQuery(status="rejected"))
            print(
                "8) list_tasks: pending =", pending.total,
                "| approved =", approved_list.total,
                "| rejected =", rejected_list.total,
                "| pendingCount =", await svc.get_pending_count(session),
            )
            assert pending.total >= 1
            assert pending.total == await svc.get_pending_count(session)
            assert all(i.review_result is None for i in pending.items)
            assert all(i.review_result == 1 for i in approved_list.items)
            assert all(i.review_result == 2 for i in rejected_list.items)

            # 9) 不存在的审核记录 → 404
            try:
                await svc.approve_review(
                    session, "1", SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME
                )
                raise AssertionError("不存在的 review 未被拦截")
            except HTTPException as e:
                assert e.status_code == 404, e.status_code
                print("9) 不存在的 review → 404 OK:", e.detail)

            # 10) 不存在的文档提审 → 404
            try:
                await svc.submit_for_review(session, "1")
                raise AssertionError("不存在的文档未被拦截")
            except HTTPException as e:
                assert e.status_code == 404, e.status_code
                print("10) 不存在的文档 → 404 OK:", e.detail)

        print("\n文档审核冒烟测试全部通过 ✅")
    finally:
        if doc_id is not None:
            async with async_session_factory() as session:
                pk = int(doc_id)
                await session.execute(delete(DocumentReview).where(DocumentReview.document_id == pk))
                await session.execute(delete(DocumentEntity).where(DocumentEntity.id == pk))
                await session.commit()
        if content_oid is not None:
            await get_document_content_collection().delete_one({"_id": content_oid})
        await close_mongo_client()
        await rabbitmq_service.close()


if __name__ == "__main__":
    asyncio.run(main())
