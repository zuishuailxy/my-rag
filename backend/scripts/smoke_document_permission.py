"""
文档端点权限冒烟（逐接口 @RequirePermission 对齐 TS 后的回归验证）

验证点：
  1  USER（补齐 document:edit / document:delete 种子后）可创建 / 列表 / 更新 / 删除文档
     —— 证明新增的 document:edit(13) / document:delete(14) 种子生效。
  2  REVIEWER（仅有 document:list / document:review，无 document:create）POST /documents
     → 403「权限不足」—— 证明 require_permission 守卫在路由层拦截。
  3  REVIEWER GET /documents → 200（仍有 document:list，无回归）。

登录态：直接用种子账号签发 JWT（smoke_auth.bearer_headers），不依赖登录接口。

注意：本脚本用 TestClient 触发 lifespan，会真的连 Mongo（创建文档需写正文）；
加 RABBITMQ_ENABLED=false 屏蔽 MQ 投递。前置 docker 的 postgres + mongodb 运行中。

用法：
    cd backend && RABBITMQ_ENABLED=false PYTHONPATH=. .venv/bin/python scripts/smoke_document_permission.py
"""
import asyncio

from bson import ObjectId
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.mongo import get_document_content_collection
from app.db.session import async_session_factory
from app.main import app
from app.models.document import DocumentEntity
from smoke_auth import (
    SEED_REVIEWER_ID,
    SEED_REVIEWER_USERNAME,
    SEED_USER_ID,
    SEED_USER_USERNAME,
    bearer_headers,
)

doc_id: str | None = None
content_oid: ObjectId | None = None


async def _cleanup() -> None:
    """物理清理测试文档（独立事件循环，lifespan dispose 后引擎自动重建连接池）"""
    global doc_id, content_oid
    if doc_id is not None:
        async with async_session_factory() as session:
            await session.execute(
                delete(DocumentEntity).where(DocumentEntity.id == int(doc_id))
            )
            await session.commit()
    if content_oid is not None:
        await get_document_content_collection().delete_one({"_id": content_oid})


def main() -> None:
    global doc_id, content_oid
    try:
        # ① USER 全流程：依赖新增的 document:edit / document:delete 种子
        with TestClient(app, headers=bearer_headers(SEED_USER_ID, SEED_USER_USERNAME)) as client:
            r = client.post(
                "/api/v1/documents",
                json={"title": "权限测试文档", "content": "正文内容", "categoryId": "500"},
            )
            print("1) USER POST /documents:", r.status_code)
            assert r.status_code == 201, r.text
            data = r.json()
            doc_id = data["id"]
            content_oid = ObjectId(data["contentId"])
            assert data["status"] == 0 and data["authorId"] == SEED_USER_ID

            r = client.get("/api/v1/documents", params={"title": "权限测试"})
            print("2) USER GET /documents:", r.status_code)
            assert r.status_code == 200

            r = client.patch(
                f"/api/v1/documents/{doc_id}", json={"title": "权限测试文档v2"}
            )
            print("3) USER PATCH /documents/{id}:", r.status_code)
            assert r.status_code == 200 and r.json()["title"] == "权限测试文档v2"

            r = client.delete(f"/api/v1/documents/{doc_id}")
            print("4) USER DELETE /documents/{id}:", r.status_code)
            assert r.status_code == 200 and r.json()["deleted"] is True
            doc_id = None  # 已软删，cleanup 不再物理删

        # ② REVIEWER 无 document:create → 创建被守卫 403
        with TestClient(
            app, headers=bearer_headers(SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME)
        ) as client:
            r = client.post(
                "/api/v1/documents",
                json={"title": "reviewer创建", "content": "正文", "categoryId": "500"},
            )
            print("5) REVIEWER POST /documents (应 403):", r.status_code)
            assert r.status_code == 403, r.text

            # ③ REVIEWER 仍有 document:list → 列表可访问（无回归）
            r = client.get("/api/v1/documents", params={"pageSize": "5"})
            print("6) REVIEWER GET /documents (应 200):", r.status_code)
            assert r.status_code == 200
    finally:
        asyncio.run(_cleanup())
    print("\n文档权限冒烟测试全部通过 ✅")


if __name__ == "__main__":
    main()
