"""
文档服务集成冒烟测试

前置条件：本地 docker 的 postgres（kh_document 表已由 init.sql 建好）与 mongodb 运行中。

用法：
    cd backend && .venv/bin/python scripts/smoke_document_service.py

测试会真实写入数据，结束时物理清理，不留脏数据。

注：update / publish / remove 的 actor 为必传参数（对应 TS 的 `actor: AuthUser`），
服务层会做写权校验（ADMIN 或作者本人）。本脚本统一用 AUTHOR 调用，故能通过；
更完整的越权矩阵由 smoke_document_publish.py（D 段 8 条）与
smoke_document_review.py（第 11 段 5 条）覆盖，本脚本只保留单点回归。
"""
import asyncio

from bson import ObjectId
from fastapi import HTTPException
from sqlalchemy import delete

from app.core.config import settings
from app.db.mongo import close_mongo_client, get_document_content_collection
from app.db.session import async_session_factory
from app.models.document import DocumentEntity, DocumentStatus
from app.schemas.auth import AuthUser
from app.schemas.document import DocumentCreate, DocumentQuery, DocumentUpdate
from app.services.document import DocumentService

# 本脚本的操作者：既是创建者（authorId 取 actor.userId）也是后续 writer
AUTHOR = AuthUser(user_id="200", username="smoke-author", roles=["ROLE_USER"])


async def main() -> None:
    svc = DocumentService()
    created_id: str | None = None
    content_oid: ObjectId | None = None
    old_flag = settings.DOCUMENT_REQUIRE_APPROVAL  # 4b 临时切免审，结束时还原
    try:
        async with async_session_factory() as session:
            # 1) 创建（草稿，不传 summary → 从正文生成 contentSummary）
            created = await svc.create(
                session,
                DocumentCreate(
                    title="集成测试文档",
                    content="你好 Hello world 这是一段用于测试的正文",
                    category_id="100",
                    author_id="200",
                    is_public=True,
                    create_by="300",
                    status=DocumentStatus.DRAFT,
                ),
                AUTHOR,
            )
            created_id = created["id"]
            content_oid = ObjectId(created["contentId"])
            mdoc = await get_document_content_collection().find_one({"_id": content_oid})
            print(
                "1) create:",
                "id=", created_id,
                "| wordCount=", created["wordCount"],
                "| Mongo contentSummary=", mdoc["contentSummary"],
                "| publishTime=", created["publishTime"],
            )
            assert created["wordCount"] == 15, created["wordCount"]  # 13 汉字 + 2 英文词
            assert created["publishTime"] is None  # 草稿无发布时间
            assert created["categoryId"] == "100"
            # authorId / createBy 均恒取 actor.userId（TS `authorId: actor.userId` /
            # `createBy: actor.userId`）—— 请求体里的 create_by=300 被服务层忽略，
            # author_id=200 恰等于 AUTHOR 的 id，故断言按 AUTHOR 校验
            assert created["authorId"] == AUTHOR.user_id
            assert created["createBy"] == AUTHOR.user_id
            assert mdoc["contentSummary"].startswith("你好 Hello world")  # 未传 summary，从正文生成

            # 2) 详情（含 Mongo 正文）
            detail = await svc.find_one(session, created_id)
            print("2) find_one:", detail["title"], "| content:", repr(detail["content"][:16]))
            assert detail["content"] == "你好 Hello world 这是一段用于测试的正文"

            # 3) 列表：标题模糊 + 状态过滤
            page = await svc.find_all(
                session,
                DocumentQuery(title="集成测试", status=DocumentStatus.DRAFT, page=1, page_size=10),
            )
            print("3) find_all: total =", page["total"], "| items =", len(page["items"]), "| pageSize =", page["pageSize"])
            assert page["total"] >= 1 and page["items"]

            # 4) 更新：改正文（状态变更不允许走 PATCH，见 update 守卫）
            updated = await svc.update(
                session,
                created_id,
                DocumentUpdate(content="新的正文内容 New content here"),
                AUTHOR,
            )
            mdoc = await get_document_content_collection().find_one({"_id": content_oid})
            print(
                "4) update(content):",
                "| Mongo version =", mdoc["version"],
                "| contentLength =", mdoc["contentLength"],
                "| publishTime =", updated["publishTime"],
            )
            assert mdoc["version"] == 2  # 正文变更 version +1
            assert updated["publishTime"] is None  # 仍为草稿，无发布时间

            # 4b) 免审模式下发布（需审模式会进 PendingReview，见 smoke_document_publish.py）
            settings.DOCUMENT_REQUIRE_APPROVAL = "false"
            published = await svc.publish(session, created_id, AUTHOR)
            print(
                "4b) publish(免审): status =", published["status"],
                "| publishTime =", published["publishTime"],
            )
            assert published["status"] == int(DocumentStatus.PUBLISHED)
            assert published["publishTime"] is not None  # 发布写入 publishTime

            # 5) 更新：只改摘要（contentSummary 同步，version 不变）
            await svc.update(session, created_id, DocumentUpdate(summary="新的摘要"), AUTHOR)
            mdoc = await get_document_content_collection().find_one({"_id": content_oid})
            print("5) update(summary): contentSummary =", mdoc["contentSummary"], "| version =", mdoc["version"])
            assert mdoc["contentSummary"] == "新的摘要"
            assert mdoc["version"] == 2

            # 5b) 非作者操作已发布文档 → 403（写权先于状态校验）
            other = AuthUser(user_id="999", username="smoke-other", roles=["ROLE_USER"])
            try:
                await svc.update(
                    session, created_id, DocumentUpdate(summary="越权改摘要"), other
                )
                raise AssertionError("5b 非作者 update 未被拦截")
            except HTTPException as e:
                assert e.status_code == 403, e.status_code
                assert e.detail == "无权修改该文档", e.detail
                print("5b) update(非作者) → 403 OK")

            # 6) 软删除 → 详情 404
            removed = await svc.remove(session, created_id, AUTHOR)
            print("6) remove:", removed)
            try:
                await svc.find_one(session, created_id)
                raise AssertionError("软删后仍能查到")
            except HTTPException as e:
                print("   软删后 find_one → 404 OK")
                assert e.status_code == 404

        print("\n集成冒烟测试全部通过 ✅")
    finally:
        settings.DOCUMENT_REQUIRE_APPROVAL = old_flag
        # 物理清理测试数据（软删记录 + Mongo 正文）
        if created_id is not None:
            async with async_session_factory() as session:
                await session.execute(
                    delete(DocumentEntity).where(DocumentEntity.id == int(created_id))
                )
                await session.commit()
        if content_oid is not None:
            await get_document_content_collection().delete_one({"_id": content_oid})
        await close_mongo_client()


if __name__ == "__main__":
    asyncio.run(main())
