"""
文档 API 冒烟测试（HTTP 层 TestClient + 真实数据库）

前置条件：docker 的 postgres + mongodb 运行中。

注意：本脚本用 TestClient 且触发 lifespan，会真的连 MQ / ES / Neo4j 并启动消费者
（KG 消费者会调 LLM）。只想验证 HTTP 语义时加 RABBITMQ_ENABLED=false 屏蔽投递。

用法：
    cd backend && RABBITMQ_ENABLED=false PYTHONPATH=. .venv/bin/python \
        scripts/smoke_document_api.py
"""
import asyncio

from bson import ObjectId
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.mongo import get_document_content_collection
from app.db.session import async_session_factory
from app.main import app
from app.models.document import DocumentEntity
from smoke_auth import SEED_ADMIN_ID, bearer_headers

doc_id: str | None = None
content_oid: ObjectId | None = None


async def _cleanup() -> None:
    """物理清理测试数据（独立事件循环，引擎已由 lifespan dispose 后自动重建连接池）"""
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
        # with 形式触发 lifespan：退出时关闭 Postgres 连接池与 Mongo 客户端
        with TestClient(app, headers=bearer_headers()) as client:
            # 1) 创建（camelCase body；建草稿）
            # 不传 status=1：DOCUMENT_REQUIRE_APPROVAL=true 时「创建即发布」会被
            # 服务层拒绝（400 开启审核时请先创建草稿），草稿路径两种模式下都成立。
            resp = client.post(
                "/api/v1/documents",
                json={
                    "title": "API测试文档",
                    "content": "你好 world 这是一段API测试正文",
                    "categoryId": "500",
                    "authorId": "600",
                    "isPublic": True,
                    "createBy": "700",
                },
            )
            print("1) POST /documents:", resp.status_code)
            assert resp.status_code == 201, resp.text
            data = resp.json()
            doc_id = data["id"]
            content_oid = ObjectId(data["contentId"])
            assert data["wordCount"] > 0 and data["publishTime"] is None
            assert data["status"] == 0
            # authorId / createBy / updateBy 三者**均恒取 JWT 中的操作者**
            # （TS `authorId: actor.userId` / `createBy: actor.userId` / `updateBy: actor.userId`）
            # —— 请求体里的 authorId=600 / createBy=700 全被服务层忽略，伪造不了
            assert data["authorId"] == SEED_ADMIN_ID
            assert data["createBy"] == SEED_ADMIN_ID
            assert data["updateBy"] == SEED_ADMIN_ID

            # 2) 列表查询：camelCase query 参数（title 模糊 + status 过滤 + pageSize）
            resp = client.get(
                "/api/v1/documents",
                params={"title": "API测试", "status": "0", "pageSize": "5"},
            )
            print("2) GET /documents?title=&status=&pageSize=:", resp.status_code)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["total"] >= 1 and body["pageSize"] == 5 and body["items"]

            # 3) 详情（带正文）
            resp = client.get(f"/api/v1/documents/{doc_id}")
            print("3) GET /documents/{id}:", resp.status_code)
            assert resp.status_code == 200, resp.text
            assert resp.json()["content"] == "你好 world 这是一段API测试正文"

            # 4) 更新（camelCase body 局部更新）
            resp = client.patch(
                f"/api/v1/documents/{doc_id}",
                json={"title": "API测试文档v2", "updateBy": "800"},
            )
            print("4) PATCH /documents/{id}:", resp.status_code)
            assert resp.status_code == 200, resp.text
            assert resp.json()["title"] == "API测试文档v2"
            # updateBy 恒取 JWT 操作者（TS `doc.updateBy = actor.userId`）——
            # 请求体里的 updateBy=800 被服务层忽略
            assert resp.json()["updateBy"] == SEED_ADMIN_ID

            # 5) 软删除
            resp = client.delete(f"/api/v1/documents/{doc_id}")
            print("5) DELETE /documents/{id}:", resp.status_code)
            assert resp.status_code == 200 and resp.json()["deleted"] is True

            # 6) 软删后详情 → 404
            resp = client.get(f"/api/v1/documents/{doc_id}")
            print("6) 软删后 GET:", resp.status_code)
            assert resp.status_code == 404

            # 7) 参数校验：pageSize 超上限 → 422
            resp = client.get("/api/v1/documents", params={"pageSize": "200"})
            print("7) pageSize=200:", resp.status_code)
            assert resp.status_code == 422

            # 8) 无效 id（超出 bigint 范围）→ 404
            resp = client.get("/api/v1/documents/9999999999999999999")
            print("8) 不存在 id:", resp.status_code)
            assert resp.status_code == 404
    finally:
        # 退出 with 后清理（lifespan 已 dispose 引擎，此处在新循环重建连接）
        asyncio.run(_cleanup())
    print("\nAPI 冒烟测试全部通过 ✅")


if __name__ == "__main__":
    main()
