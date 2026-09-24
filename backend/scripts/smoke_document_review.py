"""
文档审核服务集成冒烟测试

前置条件：本地 docker 的 postgres（kh_document / kh_document_review 已由 init.sql 建好）
与 mongodb 运行中。RabbitMQ 未启动时投递会降级为 warn，不影响断言。

用法：
    cd backend && .venv/bin/python scripts/smoke_document_review.py

测试会真实写入数据，结束时物理清理，不留脏数据。

注：submit_for_review 的 actor 是**可选**参数（)`，
与 update / remove 那批必传 actor 的写路径不同）：传了才做写权校验（ADMIN 或作者本人），
(actor && !canWriteDocument(doc, actor))` 的短路语义一致。
第 11 段覆盖该分支。
"""
import asyncio
import logging

from bson import ObjectId
from fastapi import HTTPException
from sqlalchemy import delete, select
from smoke_auth import SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME

from app.db.mongo import close_mongo_client, get_document_content_collection
from app.db.session import async_session_factory
from app.mq.rabbitmq import rabbitmq_service
from app.models.document import DocumentEntity, DocumentStatus
from app.models.document_review import DocumentReview, ReviewResult
from app.schemas.auth import AuthUser
from app.schemas.document import DocumentCreate
from app.schemas.document_review import ReviewTaskQuery
from app.services.document import DocumentService
from app.services.document_review import document_review_service as svc

# —— 写权相关身份（kh_document.author_id 无外键，可用任意雪花式 ID）——
# 文档作者：写权白名单里的「本人」
AUTHOR = AuthUser(user_id="9201", username="smoke-author", roles=["ROLE_USER"])
# 管理员：写权不受限（can_write_document 先判 ADMIN）
ADMIN_USER = AuthUser(user_id="9202", username="smoke-admin", roles=["ROLE_ADMIN"])
# 审核员：读侧不受限，但**写权白名单里没有 REVIEWER**
REVIEWER = AuthUser(user_id="9203", username="smoke-reviewer", roles=["ROLE_REVIEWER"])
# 完全无关的普通用户：对他人文档一律 403
OTHER = AuthUser(user_id="9204", username="smoke-other", roles=["ROLE_USER"])


async def main() -> None:
    # 日志
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
    # 第 11 段额外建出的文档（物理清理用）
    extra_docs: list[tuple[str, ObjectId]] = []
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
                AUTHOR,
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
            print(" 待审记录: reviewId =", current.id, "| reviewResult =", current.review_result)

            # 2) 重复提审 → 400
            # 文档已是 PendingReview，先命中 canSubmitReview 校验
            # （「该文档已有待审核任务」分支仅在状态为 Draft/Published 而流水未结案时出现）
            try:
                await svc.submit_for_review(session, doc_id)
                raise AssertionError("重复提审未被拦截")
            except HTTPException as e:
                assert e.status_code == 400, e.status_code
                print("2) 重复提审 → 400 OK:", e.detail)

            # 3) 驳回：空意见 → 400
            # 审核人必填，这里用种子 reviewer 账号
            review_id = current.id
            try:
                await svc.reject_review(
                    session, review_id, " ", SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME
                )
                raise AssertionError("空驳回意见未被拦截")
            except HTTPException as e:
                assert e.status_code == 400, e.status_code
                print("3) 空驳回意见 → 400 OK:", e.detail)

            # 4) 驳回 → Draft
            doc = await svc.reject_review(
                session,
                review_id,
                " 内容需补充 ",
                reviewer_id="9001",
                reviewer_name="张三",
            )
            print("4) reject: status =", int(doc.status), "| 期望 0（Draft）")
            assert int(doc.status) == int(DocumentStatus.DRAFT)

            history = await svc.get_review_history(session, doc_id)
            assert history[0].review_result == int(ReviewResult.REJECTED)
            assert history[0].review_comment == "内容需补充" # trim 生效
            assert history[0].reviewer_name == "张三"
            assert history[0].reviewed_at is not None
            print(" 历史首条: result =", history[0].review_result, "| comment =", history[0].review_comment)

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
            print(" 通过记录: reviewerName =", approved.reviewer_name, "| reviewerId =", approved.reviewer_id)

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

            # 11) 写权校验
            # 新建一篇 author=AUTHOR 的草稿，逐个身份尝试提审
            w_created = await DocumentService().create(
                session,
                DocumentCreate(
                    title="审核冒烟-写权文档",
                    content="写权校验用正文",
                    status=DocumentStatus.DRAFT,
                ),
                AUTHOR,
            )
            w_doc_id = w_created["id"]
            extra_docs.append((w_doc_id, ObjectId(w_created["contentId"])))

            # 11a) 无关普通用户 → 403，且零副作用（状态仍 Draft、无待审流水）
            try:
                await svc.submit_for_review(session, w_doc_id, OTHER)
                raise AssertionError("越权提审未被拦截")
            except HTTPException as e:
                assert e.status_code == 403, e.status_code
                print("11a) 无关用户提审 → 403 OK:", e.detail)
            assert (await svc.get_current_review(session, w_doc_id)) is None
            # 直查 PG 复核：越权请求零副作用（状态仍 Draft，未落审核流水）
            w_row = await session.scalar(
                select(DocumentEntity).where(DocumentEntity.id == int(w_doc_id))
            )
            assert w_row is not None and int(w_row.status) == int(DocumentStatus.DRAFT)

            # 11b) 审核员 → 403（REVIEWER 读侧不受限，但写权白名单只有 ADMIN / 作者）
            try:
                await svc.submit_for_review(session, w_doc_id, REVIEWER)
                raise AssertionError("审核员越权提审未被拦截")
            except HTTPException as e:
                assert e.status_code == 403, e.status_code
                print("11b) 审核员提审 → 403 OK:", e.detail)

            # 11c) 作者本人 → 放行，且 updateBy 记实际操作者
            w_doc = await svc.submit_for_review(session, w_doc_id, AUTHOR)
            assert int(w_doc.status) == int(DocumentStatus.PENDING_REVIEW)
            assert w_doc.update_by == int(AUTHOR.user_id)
            print(
                "11c) 作者本人提审 → PendingReview，updateBy =", w_doc.update_by,
                "（期望", int(AUTHOR.user_id), "）",
            )

            # 11d) 驳回复位后，ADMIN 提审 → 放行（写权不受归属限制）
            w_cur = await svc.get_current_review(session, w_doc_id)
            assert w_cur is not None
            await svc.reject_review(
                session, w_cur.id, "写权用例复位", SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME
            )
            w_doc = await svc.submit_for_review(session, w_doc_id, ADMIN_USER)
            assert int(w_doc.status) == int(DocumentStatus.PENDING_REVIEW)
            assert w_doc.update_by == int(ADMIN_USER.user_id)
            print("11d) ADMIN 提审 → PendingReview OK，updateBy =", w_doc.update_by)

            # 11e) actor=None → 跳过校验（(actor && ...)` 短路语义），仍可提审
            w_cur = await svc.get_current_review(session, w_doc_id)
            assert w_cur is not None
            await svc.reject_review(
                session, w_cur.id, "写权用例复位", SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME
            )
            w_doc = await svc.submit_for_review(session, w_doc_id)
            assert int(w_doc.status) == int(DocumentStatus.PENDING_REVIEW)
            print("11e) actor=None 提审 → 跳过写权校验 OK")

        print("\n文档审核冒烟测试全部通过 ✅")
    finally:
        targets: list[tuple[int, ObjectId | None]] = []
        if doc_id is not None:
            targets.append((int(doc_id), content_oid))
        targets.extend((int(i), o) for i, o in extra_docs)
        if targets:
            async with async_session_factory() as session:
                for pk, _ in targets:
                    await session.execute(
                        delete(DocumentReview).where(DocumentReview.document_id == pk)
                    )
                    await session.execute(
                        delete(DocumentEntity).where(DocumentEntity.id == pk)
                    )
                await session.commit()
            for _, oid in targets:
                if oid is not None:
                    await get_document_content_collection().delete_one({"_id": oid})
        await close_mongo_client()
        await rabbitmq_service.close()


if __name__ == "__main__":
    asyncio.run(main())
