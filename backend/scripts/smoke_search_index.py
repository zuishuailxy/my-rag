"""Search 全文检索「按 ID 拉库」链路冒烟测试（真实 PG + Mongo + ES）

验证点（对应 TS 新版：消息只传 documentId，消费者拉库后写全文）：
1. SearchIndexMessage 消息结构：只含 taskId / type / documentId（不再带 document 快照）
2. orchestrator.handle_search_index("INDEX")：按 ID 从 PG+Mongo 拉全文写入 ES kh_document，
   content 为**全文**（早期版本消息带 1000 字截断快照，正文超长部分会丢）
3. 文档不存在：走 warn 分支不抛错
4. handle_search_index("DELETE")：从 ES 移除

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_search_index.py

前置：docker 的 postgres / mongodb / elasticsearch 已就绪（连接读 backend/.env）。
"""
import asyncio
import sys
import uuid
from datetime import datetime, timezone

from bson import ObjectId
from sqlalchemy import delete as sa_delete

from app.db.mongo import close_mongo_client, get_document_content_collection
from app.db.es import es_service
from app.db.session import async_session_factory, engine
from app.models.document import DocumentEntity

# 压掉 SQLAlchemy echo 日志（import 后设置）
engine.echo = False
from app.mq.orchestrator import pipeline_orchestrator
from app.mq.rabbitmq import rabbitmq_service
from app.services.pipeline_publisher import pipeline_publisher
from app.services.search_index import DOCUMENT_INDEX

# 测试文档（固定大 ID，前置查重避免撞种子数据）
TEST_DOC_ID = 990000000000000901
# 正文 1200 字：旧方案消息快照截断到 1000 字，全文断言可验证截断缺陷已消除
FULL_CONTENT = "全文检索冒烟测试正文。" + "知识库内容片段用于验证全文写入。" * 95

CAPTURED: list[dict] = []


async def prepare() -> None:
    """插入 PG 实体 + Mongo 正文"""
    content_id = str(ObjectId())
    await get_document_content_collection().insert_one(
        {
            "_id": ObjectId(content_id),
            "documentId": str(TEST_DOC_ID),
            "content": FULL_CONTENT,
            "contentLength": len(FULL_CONTENT),
            "contentSummary": FULL_CONTENT[:50],
            "version": 1,
            "deleted": False,
        }
    )
    async with async_session_factory() as session:
        session.add(
            DocumentEntity(
                id=TEST_DOC_ID,
                title="Search 拉库冒烟文档",
                content_id=content_id,
                summary="冒烟摘要",
                status=1,  # Published
                is_public=True,
                tags="smoke",
                deleted=False,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        await session.commit()
    print(f"   准备文档：documentId={TEST_DOC_ID}, contentId={content_id}")


async def cleanup() -> None:
    """物理清理：ES → Mongo → PG（先子后主，幂等）"""
    c = es_service.client
    if c is not None:
        try:
            await c.delete(index=DOCUMENT_INDEX, id=str(TEST_DOC_ID), refresh=True)
        except Exception:
            pass
    async with async_session_factory() as session:
        result = await session.execute(
            sa_delete(DocumentEntity).where(DocumentEntity.id == TEST_DOC_ID)
        )
        await session.commit()  # 不 commit 会被回滚，留下残留行
        deleted_pg = int(result.rowcount or 0)  # type: ignore[attr-defined]
    removed_mongo = await get_document_content_collection().delete_one(
        {"documentId": str(TEST_DOC_ID)}
    )
    print(
        f"   清理：PG 删 {deleted_pg} 行，Mongo 删 {removed_mongo.deleted_count} 条，ES 已删索引记录"
    )


async def main() -> None:
    print("=== Search 按 ID 拉库链路冒烟 ===")

    # 0) 环境自检
    await es_service.start()
    assert es_service.client is not None, "ES 客户端不可用，请检查 docker/elasticsearch"

    # 1) 消息结构：只带 documentId（消费者拉库，不再内嵌快照）
    orig_publish = rabbitmq_service.publish

    async def spy_publish(exchange: str, rk: str, body: object) -> bool:
        CAPTURED.append(dict(body))  # type: ignore[call-overload]
        return await orig_publish(exchange, rk, body)

    rabbitmq_service.publish = spy_publish  # type: ignore[method-assign,assignment]
    await pipeline_publisher._trigger_search_index(TEST_DOC_ID)
    assert len(CAPTURED) == 1, f"应捕获 1 条消息，实际 {len(CAPTURED)}"
    body = CAPTURED[0]
    assert set(body.keys()) == {"taskId", "type", "documentId"}, (
        f"消息应只含 taskId/type/documentId，实际 keys={sorted(body.keys())}"
    )
    assert body["type"] == "INDEX" and body["documentId"] == str(TEST_DOC_ID)
    uuid.UUID(body["taskId"])  # taskId 必须是合法 uuid
    print("   1) 消息结构 ✓ 仅 taskId/type/documentId，无 document 快照")

    await prepare()

    # 2) INDEX：按 ID 拉库 → ES kh_document，content 为全文
    await pipeline_orchestrator.handle_search_index("INDEX", str(TEST_DOC_ID))
    got = await es_service.client.get(index=DOCUMENT_INDEX, id=str(TEST_DOC_ID))
    doc = got["_source"]
    assert doc["content"] == FULL_CONTENT, (
        f"ES content 应为全文（len={len(FULL_CONTENT)}），"
        f"实际 len={len(doc.get('content') or '')}"
    )
    assert doc["title"] == "Search 拉库冒烟文档"
    assert doc["status"] == 1 and doc["isPublic"] is True
    assert doc["categoryId"] is None and doc["authorId"] is None
    assert doc.get("indexedAt"), "indexedAt 应由 SearchIndexService 补充"
    print(f"   2) INDEX ✓ ES content 全文 len={len(doc['content'])}（>1000 不截断）")

    # 3) 文档不存在：warn 分支不抛错
    await pipeline_orchestrator.handle_search_index("INDEX", "990000000000000999")
    got_again = await es_service.client.get(
        index=DOCUMENT_INDEX, id=str(TEST_DOC_ID)
    )
    assert got_again["_source"]["title"] == "Search 拉库冒烟文档"
    print("   3) 文档不存在 ✓ 走 warn 分支不抛错、不误写")

    # 4) DELETE：从 ES 移除
    await pipeline_orchestrator.handle_search_index("DELETE", str(TEST_DOC_ID))
    exists = await es_service.client.exists(index=DOCUMENT_INDEX, id=str(TEST_DOC_ID))
    assert not exists, "DELETE 后 ES 记录应已移除"
    print("   4) DELETE ✓ ES 记录已移除")

    await cleanup()
    print("\nSearch 按 ID 拉库链路冒烟测试全部通过 ✅")


async def run() -> None:
    """main + cleanup 同一事件循环（engine 连接池绑定首个 loop，不能二次 run）"""
    try:
        await main()
    except Exception:
        print("冒烟测试失败，执行清理", file=sys.stderr)
        await cleanup()
        raise
    finally:
        await es_service.close()
        await close_mongo_client()


if __name__ == "__main__":
    asyncio.run(run())
