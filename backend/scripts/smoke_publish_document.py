"""
发布文档冒烟测试（PUT /documents/{id}/publish）

覆盖：
- 草稿 → 发布：status=Published、publishTime 写入、返回正文；
- 已发布再发布：幂等（200，重建索引语义）；
- 已归档文档发布 → 400；
- MQ 投递为占位实现（Noop，仅记日志），不影响发布成功。

用法：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/smoke_publish_document.py
"""
import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.mongo import get_document_content_collection
from app.db.session import async_session_factory
from app.main import app
from app.models.document import DocumentEntity

doc_id: str | None = None


async def _cleanup() -> None:
    global doc_id
    if doc_id is not None:
        async with async_session_factory() as session:
            await session.execute(
                delete(DocumentEntity).where(DocumentEntity.id == int(doc_id))
            )
            await session.commit()
        await get_document_content_collection().delete_one({"documentId": str(doc_id)})


def main() -> None:
    global doc_id
    try:
        with TestClient(app) as client:
            # 1) 草稿 → 发布
            r = client.post(
                "/api/v1/documents",
                json={"title": "发布测试", "content": "正文内容 hello"},
            )
            assert r.status_code == 201, r.text
            doc_id = r.json()["id"]
            assert r.json()["status"] == 0  # 草稿

            r = client.put(f"/api/v1/documents/{doc_id}/publish")
            print("1) PUT /publish:", r.status_code)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["status"] == 1, "应为已发布"
            assert data["publishTime"] is not None, "publishTime 未写入"
            assert data["content"] == "正文内容 hello", "返回正文缺失"
            print("   status=1, publishTime=", data["publishTime"])

            # 2) 已发布再发布 → 幂等 200
            r = client.put(f"/api/v1/documents/{doc_id}/publish")
            print("2) 重复发布:", r.status_code)
            assert r.status_code == 200 and r.json()["status"] == 1

            # 3) 已归档文档发布 → 400
            #    先归档（用 update 改 status=2），再发布应被拒
            r = client.patch(
                f"/api/v1/documents/{doc_id}", json={"status": 2}
            )
            assert r.status_code == 200 and r.json()["status"] == 2
            r = client.put(f"/api/v1/documents/{doc_id}/publish")
            print("3) 归档后发布:", r.status_code)
            assert r.status_code == 400 and "不允许发布" in r.json()["detail"]

            # 清理：软删
            client.delete(f"/api/v1/documents/{doc_id}")
    finally:
        asyncio.run(_cleanup())
    print("\n发布文档冒烟测试全部通过 ✅")


if __name__ == "__main__":
    main()
