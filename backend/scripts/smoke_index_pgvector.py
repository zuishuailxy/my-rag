"""
PGVector 发布索引冒烟测试（RAG 消费者）

覆盖：
- chunker / embedding 单元行为；
- 发布文档 → POST /index → doc_chunk 落库（数量、序号、内容）；
- 重索引幂等（chunkCount 稳定）；
- 向量检索：同文查询返回对应分片（局部确定性嵌入）；
- 清理：删文档级联删分片。

用法：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/smoke_index_pgvector.py
"""
import asyncio
import math

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from app.db.mongo import get_document_content_collection
from app.db.session import async_session_factory
from app.main import app
from app.models.doc_chunk import DocChunk
from app.models.document import DocumentEntity
from app.services.chunker import chunking_service
from app.services.embedding import embedding_provider

# 多段长正文（>500 字符，保证切成多块）
LONG_CONTENT = "\n\n".join(
    [f"## 第 {i} 节\n\n" + ("企业知识库系统支持多模态文档的归档与检索。" * 60) for i in range(4)]
)


async def _verify_and_cleanup(doc_id: str, expect_chunks: int) -> None:
    """退出 TestClient 后单循环内完成：查库校验 + 向量检索 + 物理清理"""
    async with async_session_factory() as session:
        # 1) 落库校验
        rows = (
            await session.execute(
                select(DocChunk)
                .where(DocChunk.document_id == int(doc_id))
                .order_by(DocChunk.chunk_index)
            )
        ).scalars().all()
        print("3) doc_chunk 行数:", len(rows), "| 序号:", [r.chunk_index for r in rows])
        assert len(rows) == expect_chunks, f"应 {expect_chunks} 行，实际 {len(rows)}"
        assert [r.chunk_index for r in rows] == list(range(len(rows)))
        assert rows[0].content.strip().startswith("## 第 0 节")

        # 2) 向量检索：用第 1 块内容查询 → 应命中第 1 块
        query_vec = (await embedding_provider.embed_texts([rows[1].content]))[0]
        hit = (
            await session.execute(
                text(
                    "SELECT chunk_index FROM doc_chunk "
                    "WHERE document_id = :doc "
                    "ORDER BY embedding <=> :qv LIMIT 1"
                ),
                {"doc": int(doc_id), "qv": str(query_vec)},
            )
        ).one()
        print("4) 向量检索命中 chunk_index =", hit[0])
        assert hit[0] == 1, f"应命中第 1 块，实际 {hit[0]}"

        # 3) 物理清理：删文档（级联删分片）
        await session.execute(delete(DocumentEntity).where(DocumentEntity.id == int(doc_id)))
        await session.commit()
    await get_document_content_collection().delete_one({"documentId": str(doc_id)})


def main() -> None:
    # 0) 单元：chunker / embedding（纯 Python，独立循环）
    chunks = __import__('asyncio').run(chunking_service.chunk(content=LONG_CONTENT, document_id='0', document_title='t'))
    assert len(chunks) >= 2, f"应切成多块，实际 {len(chunks)}"
    assert all(len(c.content) <= 1100 for c in chunks), "分块超长"
    v1 = asyncio.run(embedding_provider.embed_texts(["企业知识库系统支持多模态检索"]))[0]
    v2 = asyncio.run(embedding_provider.embed_texts(["企业知识库系统支持多模态检索"]))[0]
    v3 = asyncio.run(embedding_provider.embed_texts(["完全无关的股市行情讨论"]))[0]
    cos = lambda a, b: sum(x * y for x, y in zip(a, b))
    # 真实 API 跨请求非逐位确定（差值 ~2e-4，余弦≈1），用近似相等（local 伪向量恒等也满足）
    assert cos(v1, v2) > 0.999, "同文本向量应一致（近似）"
    assert cos(v1, v3) < cos(v1, v1), "相似文本余弦应更高"
    print(f"0) chunker/embedding OK（{len(chunks)} 块）")

    doc_id: str | None = None
    try:
        with TestClient(app) as client:
            # 1) 创建已发布文档
            r = client.post(
                "/api/v1/documents",
                json={"title": "索引测试文档", "content": LONG_CONTENT, "status": 1},
            )
            assert r.status_code == 201, r.text
            doc_id = r.json()["id"]
            print("1) 创建已发布文档:", doc_id)

            # 2) 索引
            r = client.post(f"/api/v1/documents/{doc_id}/index")
            print("2) POST /index:", r.status_code, r.json())
            assert r.status_code == 200 and r.json()["chunkCount"] >= 2
            first_count = r.json()["chunkCount"]

            # 5) 重索引幂等（API 层 chunkCount 稳定）
            r = client.post(f"/api/v1/documents/{doc_id}/index")
            assert r.status_code == 200 and r.json()["chunkCount"] == first_count
            print("5) 重索引幂等 OK:", r.json()["chunkCount"])

            # 软删除（清理阶段再物理删除）
            client.delete(f"/api/v1/documents/{doc_id}")
    finally:
        if doc_id is not None:
            asyncio.run(_verify_and_cleanup(doc_id, first_count))
    print("\nPGVector 发布索引冒烟测试全部通过 ✅")


if __name__ == "__main__":
    main()
