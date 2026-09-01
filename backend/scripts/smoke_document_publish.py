"""
文档发布 / 归档 / 保存草稿 集成冒烟测试（对应 TS DocumentService.publish / archive /
saveAsDraft / remove / update 的状态与索引同步分支）

前置条件：本地 docker 的 postgres（kh_document / kh_document_review 已由 init.sql 建好）
与 mongodb 运行中。RabbitMQ 未启动时不报错（服务内部降级为 warn），但断言依赖
after_publish / after_unpublish 的**调用次数**，与 MQ 是否可达无关。

用法：
    cd backend && .venv/bin/python scripts/smoke_document_publish.py

测试会真实写入数据，结束时物理清理，不留脏数据。
"""
import asyncio
import logging

from bson import ObjectId
from fastapi import HTTPException
from sqlalchemy import delete

from app.core.config import settings
from app.db.mongo import close_mongo_client, get_document_content_collection
from app.db.session import async_session_factory
from app.models.document import DocumentEntity, DocumentStatus
from app.models.document_review import DocumentReview
from app.mq.rabbitmq import rabbitmq_service
from app.schemas.document import DocumentCreate, DocumentUpdate
from app.services.document import DocumentService
from app.services.document_review import document_review_service
from app.services.pipeline_publisher import pipeline_publisher

# after_publish / after_unpublish 调用记录（spy 包一层，仍然真实投递）
PUBLISHED: list[str] = []
UNPUBLISHED: list[str] = []


def _install_spy() -> None:
    """包装管线投递方法：记录调用并透传给真实实现"""
    orig_publish = pipeline_publisher.after_publish
    orig_unpublish = pipeline_publisher.after_unpublish

    async def spy_publish(document, content=None):  # type: ignore[no-untyped-def]
        PUBLISHED.append(str(document.id))
        return await orig_publish(document, content)

    async def spy_unpublish(document_id):  # type: ignore[no-untyped-def]
        UNPUBLISHED.append(str(document_id))
        return await orig_unpublish(document_id)

    pipeline_publisher.after_publish = spy_publish  # type: ignore[method-assign]
    pipeline_publisher.after_unpublish = spy_unpublish  # type: ignore[method-assign]


async def _aexpect_400(awaitable, label: str, detail_contains: str) -> None:
    """断言 awaitable 抛出 400 且 detail 包含指定文案"""
    try:
        await awaitable
    except HTTPException as e:
        assert e.status_code == 400, f"{label}: 期望 400，实际 {e.status_code}"
        assert detail_contains in str(e.detail), f"{label}: detail={e.detail}"
        print(f"   ✓ {label} → 400：{e.detail}")
        return
    raise AssertionError(f"{label} 未被拦截")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
    )
    logging.getLogger("sqlalchemy.engine").disabled = True
    logging.getLogger("sqlalchemy.engine.Engine").disabled = True

    await rabbitmq_service.start()
    _install_spy()

    svc = DocumentService()
    created_ids: list[str] = []
    content_oids: list[ObjectId] = []
    old_flag = settings.DOCUMENT_REQUIRE_APPROVAL

    try:
        async with async_session_factory() as session:
            # ============ A. 需审模式（DOCUMENT_REQUIRE_APPROVAL=true）============
            settings.DOCUMENT_REQUIRE_APPROVAL = "true"
            print("\n=== A. 需审模式 ===")
            print("A0) is_require_approval =", document_review_service.is_require_approval())
            assert document_review_service.is_require_approval() is True

            # A1) 创建时不允许 Archived / PendingReview
            await _aexpect_400(
                svc.create(
                    session,
                    DocumentCreate(
                        title="冒烟-归档态", content="正文", status=DocumentStatus.ARCHIVED
                    ),
                ),
                "A1 create(Archived)",
                "创建文档仅允许草稿或已发布状态",
            )

            # A2) 需审模式下不允许创建即发布
            await _aexpect_400(
                svc.create(
                    session,
                    DocumentCreate(
                        title="冒烟-创建即发布",
                        content="正文",
                        status=DocumentStatus.PUBLISHED,
                    ),
                ),
                "A2 create(Published)",
                "开启审核时请先创建草稿",
            )

            # A3) 正常建草稿
            doc = await svc.create(
                session,
                DocumentCreate(
                    title="冒烟-发布流程",
                    content="发布流程验证正文",
                    status=DocumentStatus.DRAFT,
                ),
            )
            doc_id = doc["id"]
            created_ids.append(doc_id)
            content_oids.append(ObjectId(doc["contentId"]))
            print(f"A3) create(Draft): id={doc_id} status={doc['status']}")
            assert doc["status"] == int(DocumentStatus.DRAFT)

            # A4) publish（需审）→ PendingReview，不建索引
            before = len(PUBLISHED)
            res = await svc.publish(session, doc_id)
            print(f"A4) publish → status={res['status']}（期望 3 PendingReview）")
            assert res["status"] == int(DocumentStatus.PENDING_REVIEW)
            assert len(PUBLISHED) == before, "需审提审不应建索引"

            # A5) 审核中再 publish → 400
            await _aexpect_400(
                svc.publish(session, doc_id), "A5 publish(审核中)", "文档审核中，请等待审核结果"
            )

            # A6/A7) 审核中不可改正文 / 标题
            await _aexpect_400(
                svc.update(session, doc_id, DocumentUpdate(content="改稿")),
                "A6 update(改正文)",
                "审核中的文档不可编辑",
            )
            await _aexpect_400(
                svc.update(session, doc_id, DocumentUpdate(title="改标题")),
                "A7 update(改标题)",
                "审核中的文档不可编辑",
            )

            # A8) 审核中允许改元数据（remark）
            res = await svc.update(session, doc_id, DocumentUpdate(remark="审核中备注"))
            print(f"A8) update(remark): remark={res['remark']} status={res['status']}")
            assert res["remark"] == "审核中备注"
            assert res["status"] == int(DocumentStatus.PENDING_REVIEW)

            # A9) PATCH 不允许 PendingReview→Draft
            await _aexpect_400(
                svc.update(session, doc_id, DocumentUpdate(status=DocumentStatus.DRAFT)),
                "A9 update(status=Draft)",
                "请使用 publish / archive / save-draft / 审核接口变更文档状态",
            )

            # A10) 审核通过 → Published + 建索引
            before = len(PUBLISHED)
            current = await document_review_service.get_current_review(session, doc_id)
            assert current is not None
            approved = await document_review_service.approve_review(
                session, current.id, reviewer_id="9001", reviewer_name="李四"
            )
            print(
                f"A10) approve → status={int(approved.status)}"
                f" publishTime={approved.publish_time}"
            )
            assert int(approved.status) == int(DocumentStatus.PUBLISHED)
            assert len(PUBLISHED) == before + 1, "审核通过应建索引"

            # A11) Published→Draft（PATCH 唯一允许的状态变更）→ 清索引
            before = len(UNPUBLISHED)
            res = await svc.update(
                session, doc_id, DocumentUpdate(status=DocumentStatus.DRAFT)
            )
            print(f"A11) update( Published→Draft ): status={res['status']}（期望 0）")
            assert res["status"] == int(DocumentStatus.DRAFT)
            assert len(UNPUBLISHED) == before + 1, "下架应清索引"

            # A12) Draft→Published 不允许走 PATCH
            await _aexpect_400(
                svc.update(
                    session, doc_id, DocumentUpdate(status=DocumentStatus.PUBLISHED)
                ),
                "A12 update(status=Published)",
                "请使用 publish / archive / save-draft / 审核接口变更文档状态",
            )

            # ============ B. 免审模式（DOCUMENT_REQUIRE_APPROVAL=false）============
            settings.DOCUMENT_REQUIRE_APPROVAL = "false"
            print("\n=== B. 免审模式 ===")
            assert document_review_service.is_require_approval() is False

            # B1) 免审可创建即发布，并立即建索引
            before = len(PUBLISHED)
            doc2 = await svc.create(
                session,
                DocumentCreate(
                    title="冒烟-免审发布",
                    content="免审模式正文",
                    status=DocumentStatus.PUBLISHED,
                ),
            )
            doc2_id = doc2["id"]
            created_ids.append(doc2_id)
            content_oids.append(ObjectId(doc2["contentId"]))
            print(
                f"B1) create(Published): status={doc2['status']}"
                f" publishTime={doc2['publishTime']}"
            )
            assert doc2["status"] == int(DocumentStatus.PUBLISHED)
            assert doc2["publishTime"] is not None
            assert len(PUBLISHED) == before + 1, "创建即发布应建索引"

            # B2) 已发布改正文 → 免审重建索引
            before = len(PUBLISHED)
            res = await svc.update(
                session, doc2_id, DocumentUpdate(content="免审模式改稿后正文")
            )
            print(f"B2) update(改正文): status={res['status']} content={res['content'][:8]}")
            assert len(PUBLISHED) == before + 1, "免审模式下改稿应重建索引"

            # B3) publish（已发布）→ 仍 Published + 重建
            before = len(PUBLISHED)
            res = await svc.publish(session, doc2_id)
            print(f"B3) publish(已发布): status={res['status']}")
            assert res["status"] == int(DocumentStatus.PUBLISHED)
            assert len(PUBLISHED) == before + 1

            # B4) 归档 → Archived + 清索引
            before = len(UNPUBLISHED)
            res = await svc.archive(session, doc2_id)
            print(f"B4) archive: status={res['status']}（期望 2 Archived）")
            assert res["status"] == int(DocumentStatus.ARCHIVED)
            assert len(UNPUBLISHED) == before + 1

            # B5) 非已发布归档 → 400
            await _aexpect_400(
                svc.archive(session, doc2_id), "B5 archive(非已发布)", "只有已发布文档可以归档"
            )

            # B6) Archived 可重新上架
            before = len(PUBLISHED)
            res = await svc.publish(session, doc2_id)
            print(f"B6) publish(已归档): status={res['status']}（期望 1 Published）")
            assert res["status"] == int(DocumentStatus.PUBLISHED)
            assert len(PUBLISHED) == before + 1

            # B7) 保存草稿 → Draft + 清索引
            before = len(UNPUBLISHED)
            res = await svc.save_as_draft(session, doc2_id)
            print(f"B7) save_as_draft: status={res['status']}（期望 0 Draft）")
            assert res["status"] == int(DocumentStatus.DRAFT)
            assert len(UNPUBLISHED) == before + 1

            # B8) 非已发布保存草稿 → 400
            await _aexpect_400(
                svc.save_as_draft(session, doc2_id),
                "B8 save_as_draft(非已发布)",
                "只有已发布文档可以保存为草稿",
            )

            # ============ C. 删除的索引清理条件 ============
            print("\n=== C. 软删除 ===")

            # C1) 草稿删除：不投递 unpublish
            before = len(UNPUBLISHED)
            removed = await svc.remove(session, doc_id)
            print(f"C1) remove(Draft): {removed}")
            assert removed["deleted"] is True
            assert len(UNPUBLISHED) == before, "草稿删除不应清索引"

            # C2) 已发布删除：投递 unpublish
            before = len(UNPUBLISHED)
            doc3 = await svc.create(
                session,
                DocumentCreate(
                    title="冒烟-删除清索引",
                    content="删除验证正文",
                    status=DocumentStatus.PUBLISHED,
                ),
            )
            created_ids.append(doc3["id"])
            content_oids.append(ObjectId(doc3["contentId"]))
            await svc.remove(session, doc3["id"])
            print(f"C2) remove(Published): unpublish 投递 = {len(UNPUBLISHED) - before}")
            assert len(UNPUBLISHED) == before + 1, "已发布删除应清索引"

        print(
            f"\n投递统计：after_publish × {len(PUBLISHED)}，"
            f"after_unpublish × {len(UNPUBLISHED)}"
        )
        print("\n文档发布 / 归档 / 草稿冒烟测试全部通过 ✅")
    finally:
        settings.DOCUMENT_REQUIRE_APPROVAL = old_flag
        for oid in content_oids:
            await get_document_content_collection().delete_one({"_id": oid})
        async with async_session_factory() as session:
            for did in created_ids:
                pk = int(did)
                await session.execute(
                    delete(DocumentReview).where(DocumentReview.document_id == pk)
                )
                await session.execute(
                    delete(DocumentEntity).where(DocumentEntity.id == pk)
                )
            await session.commit()
        await close_mongo_client()
        await rabbitmq_service.close()


if __name__ == "__main__":
    asyncio.run(main())
