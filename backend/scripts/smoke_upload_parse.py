"""
上传并解析端点冒烟测试（HTTP 层 TestClient + 真实数据库 + 真实 RustFS）

覆盖：
- .md 文件上传 → 201，创建草稿 + 原文件入 RustFS + 返回摘要；
- 不支持的扩展名 → 400；
- 空文件 → 400；
- 清理：文档（PG+Mongo）+ RustFS 对象。

用法：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/smoke_upload_parse.py
"""
import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.mongo import get_document_content_collection
from app.db.session import async_session_factory
from app.main import app
from app.models.document import DocumentEntity
from app.services.rustfs import rustfs_service
from smoke_auth import bearer_headers

doc_id: str | None = None
rustfs_keys: list[str] = []


async def _cleanup() -> None:
    global doc_id, rustfs_keys
    if doc_id is not None:
        async with async_session_factory() as session:
            await session.execute(
                delete(DocumentEntity).where(DocumentEntity.id == int(doc_id))
            )
            await session.commit()
        await get_document_content_collection().delete_one({"documentId": str(doc_id)})
    # 清理 RustFS 对象（从 URL 解析 key）
    if rustfs_service._client is not None:
        for key in rustfs_keys:
            await asyncio.to_thread(
                rustfs_service._client.delete_object, Bucket="knowledge-hub", Key=key
            )


def main() -> None:
    global doc_id, rustfs_keys
    try:
        with TestClient(app, headers=bearer_headers()) as client:
            # 1) 上传 .md → 201
            content = "# 部署指南\n\n1. docker compose up -d\n2. 初始化数据库"
            resp = client.post(
                "/api/v1/documents/upload/parse",
                files={"file": ("部署指南.md", content.encode("utf-8"), "text/markdown")},
                data={"categoryId": "100", "createBy": "200", "isPublic": "true"},
            )
            print("1) POST /upload/parse:", resp.status_code)
            assert resp.status_code == 201, resp.text
            data = resp.json()
            doc_id = data["documentId"]
            assert data["title"] == "部署指南", data["title"]
            assert data["fileExtension"] == "md"
            assert data["fileSize"] == len(content.encode("utf-8"))
            assert data["contentLength"] == len(content)
            assert data["contentPreview"] == content[:200]
            assert data["status"] == 0
            assert data["fileUrl"].startswith(
                "http://127.0.0.1:9000/knowledge-hub/documents/"
            ), data["fileUrl"]
            # 记录 RustFS key 用于清理
            rustfs_keys.append(data["fileUrl"].split("/knowledge-hub/")[-1])
            print("   title=%s, fileUrl=%s" % (data["title"], data["fileUrl"]))

            # 2) 文档确实已创建（详情接口）
            r = client.get(f"/api/v1/documents/{doc_id}")
            assert r.status_code == 200, r.text
            assert r.json()["content"] == content
            assert r.json()["status"] == 0  # 草稿
            print("2) 草稿已创建，正文一致 OK")

            # 3) 不支持的扩展名 → 400
            r = client.post(
                "/api/v1/documents/upload/parse",
                files={"file": ("evil.exe", b"MZ...", "application/octet-stream")},
            )
            print("3) 不支持格式:", r.status_code)
            assert r.status_code == 400 and "不支持的文件格式" in r.json()["detail"]

            # 4) 空文件 → 400
            r = client.post(
                "/api/v1/documents/upload/parse",
                files={"file": ("empty.txt", b"", "text/plain")},
            )
            print("4) 空文件:", r.status_code)
            assert r.status_code == 400 and "文件不能为空" in r.json()["detail"]
    finally:
        asyncio.run(_cleanup())
    print("\n上传解析冒烟测试全部通过 ✅")


if __name__ == "__main__":
    main()
