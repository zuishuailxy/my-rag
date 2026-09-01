"""
发布文档冒烟测试（PUT /documents/{id}/publish）

覆盖：
- 草稿 → 发布：status=Published、publishTime 写入、返回正文；
- 已发布再发布：幂等（200，重建索引语义）；
- 已归档文档 → 重新上架（can_publish_from 允许 Archived，200 回到 Published）；
- MQ 投递为占位实现（Noop，仅记日志），不影响发布成功。

⚠️ 强制免审（见下方 settings 覆写）：本脚本验证「发布的状态流转」，不覆盖审核链路
（审核由 smoke_document_review*.py 覆盖）。需审模式下草稿 publish 会进 PendingReview
而非 Published，第 1 步的 status==1 断言必然失败。

用法：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/smoke_publish_document.py
"""
import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.config import settings
from app.db.mongo import get_document_content_collection
from app.db.session import async_session_factory
from app.main import app
from app.models.document import DocumentEntity
from smoke_auth import bearer_headers

# 测试夹具：关掉审核开关（DOCUMENT_REQUIRE_APPROVAL 判定严格等于 "false" 才免审）
settings.DOCUMENT_REQUIRE_APPROVAL = "false"

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
        with TestClient(app, headers=bearer_headers()) as client:
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

            # 3) 归档 → 重新上架（can_publish_from 允许 Archived 作为来源，TS 同款；
            #    Archived 即使需审也走 direct_publish 直接上架）
            #    归档必须走 archive 接口：PATCH 不允许改 status
            #    （唯一例外是 Published→Draft，见「文档模块CRUD」约定）
            r = client.put(f"/api/v1/documents/{doc_id}/archive")
            assert r.status_code == 200 and r.json()["status"] == 2, r.text
            r = client.put(f"/api/v1/documents/{doc_id}/publish")
            print("3) 归档后重新发布:", r.status_code)
            assert r.status_code == 200 and r.json()["status"] == 1, r.text

            # 清理：软删
            client.delete(f"/api/v1/documents/{doc_id}")
    finally:
        asyncio.run(_cleanup())
    print("\n发布文档冒烟测试全部通过 ✅")


if __name__ == "__main__":
    main()
