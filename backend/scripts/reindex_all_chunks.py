"""全量重建 RAG 索引（PG 向量 + ES kh_chunk）与文档级搜索索引（ES kh_document）

背景一（kh_chunk）：检索可见性过滤依赖 chunk 上的 is_public / author_id / team_id。
早于该特性入库的 chunk 没有 is_public，加过滤后非管理员将召回不到它们
（term is_public=true 匹配不到缺失字段）。

背景二（kh_document）：文档级全文检索（POST /search）的可见性过滤依赖
isPublic / authorId / teamId（camelCase）。早于该特性入库的文档记录没有 teamId，
加过滤后非管理员将搜不到团队可见的文档。ES 里没有 teamId 来源，必须回 PG 重拉重灌。

做法：对全部已发布文档对象走同一份快照——
  - reindex_document：重新分块 → 重新嵌入 → 覆盖 PG doc_chunk + ES kh_chunk
  - to_search_index_doc + index_document：按与编排器完全相同的字段映射重灌 kh_document
（复用 services 层与 pipeline.types 的公共实现，不做第二套逻辑 —— 单一事实源）

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/reindex_all_chunks.py [--dry-run]
        [--skip-chunks] [--skip-documents]

--dry-run 只列出将重建的文档，不写任何数据。
--skip-chunks / --skip-documents 可只补其中一个索引（chunk 侧重嵌入有 API 成本）。
"""
import argparse
import asyncio
import logging
import sys

sys.path.insert(0, ".")

from app.db.es import es_service # noqa: E402
from app.db.session import async_session_factory # noqa: E402
from app.pipeline.types import PipelineDocument, to_search_index_doc # noqa: E402
from app.services.es_index import es_chunk_index_service # noqa: E402
from app.services.indexing import ( # noqa: E402
    load_all_published_documents,
    reindex_document,
)
from app.services.search_index import search_index_service # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ReindexAllChunks")


async def _reindex_one(
    doc: PipelineDocument, skip_chunks: bool, skip_documents: bool, attempts: int = 3
) -> int:
    """单篇重建（**独立会话**，带重试）；返回该篇 chunk 数

    PG 因资源压力崩溃时连接会失效；独立会话 + 重试可把影响局限在单篇，
    而不是让整批一起失败。
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with async_session_factory() as session:
                count = 0
                if not skip_chunks:
                    count = await reindex_document(session, doc)
                if not skip_documents:
                    await search_index_service.index_document(to_search_index_doc(doc))
                return count
        except Exception as err:  # noqa: BLE001
            last_error = err
            if attempt < attempts:
                logger.warning(
                    "第 %d/%d 次失败，重试：documentId=%s, %s",
                    attempt,
                    attempts,
                    doc.id,
                    str(err)[:120],
                )
                await asyncio.sleep(5)
    raise last_error  # type: ignore[misc]


async def main(
    dry_run: bool, skip_chunks: bool = False, skip_documents: bool = False
) -> int:
    # ES 客户端必须显式初始化：本脚本不挂 lifespan，不 start 会导致
    # 「跳过写入（ES 不可用）」——两个索引的可见性字段就都补不上
    await es_service.start()
    if es_service.client is None:
        logger.error("ES 不可用：索引侧无法重建，请先启动 elasticsearch 后重跑")
        return 2

    async with async_session_factory() as session:
        docs = await load_all_published_documents(session)
        logger.info("已发布文档 %d 篇待重建", len(docs))
        if dry_run:
            for doc in docs:
                logger.info(
                    " [dry-run] id=%s title=%s isPublic=%s teamId=%s authorId=%s",
                    doc.id,
                    doc.title,
                    doc.is_public,
                    doc.team_id,
                    doc.author_id,
                )
            return 0

        # 先补两边 ES mapping（老索引分别缺 is_public / teamId，写入前必须存在，
        # 否则字段被丢弃或落到动态映射的 text 上、使 terms 过滤静默失效）
        if not skip_chunks:
            await es_chunk_index_service.ensure_visibility_mapping()
        if not skip_documents:
            await search_index_service.ensure_visibility_mapping()

        total_chunks = 0
        indexed_docs = 0
        failed: set[str] = set()
        for doc in docs:
            # ⚠️ **每篇用独立会话 + 重试**。
            # 教训：原先 174 篇共用一个会话，PG 一旦崩（本机资源紧张时半小时内可崩 6 次），
            # 这个会话就永久失效 —— 实测一次崩把后面 74 篇全部拖死，索引被留在混合尺寸。
            # 改成每篇独立会话后，一次崩最多损失一篇（且会自动重试），其余继续推进。
            try:
                count = await _reindex_one(doc, skip_chunks, skip_documents)
                if not skip_chunks:
                    total_chunks += count
                if not skip_documents:
                    indexed_docs += 1
            except Exception as err:  # noqa: BLE001 —— 单篇失败不阻断其余文档
                failed.add(str(doc.id))
                logger.error("重建失败（已重试）：documentId=%s, %s", doc.id, str(err)[:160])

        logger.info(
            "全量重建结束：文档=%d, chunk 总数=%d, 文档索引=%d, 失败=%d",
            len(docs),
            total_chunks,
            indexed_docs,
            len(failed),
        )
        if failed:
            logger.error("失败文档：%s", ", ".join(sorted(failed)))
            return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="全量重建 kh_chunk 与 kh_document（补齐可见性字段）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只列出待重建文档，不写数据"
    )
    parser.add_argument(
        "--skip-chunks",
        action="store_true",
        help="跳过 chunk 侧（不重新分块嵌入），只补 kh_document",
    )
    parser.add_argument(
        "--skip-documents",
        action="store_true",
        help="跳过文档级索引，只重建 kh_chunk",
    )
    args = parser.parse_args()

    async def _run() -> int:
        try:
            return await main(
                args.dry_run,
                skip_chunks=args.skip_chunks,
                skip_documents=args.skip_documents,
            )
        finally:
            # 关闭 ES 连接，避免 aiohttp "Unclosed client session" 告警
            await es_service.close()

    raise SystemExit(asyncio.run(_run()))
