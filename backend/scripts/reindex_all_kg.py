"""全量重建 KG 知识图谱（Neo4j）

背景：图谱只在**发布时**经 MQ orchestrator → `graph_build_service.build_batch` 建。
改了抽取配置（`KG_MAX_ENTITIES` / `KG_MAX_RELATIONS`）后，旧图仍是**旧上限**抽的，
必须重跑本脚本才生效 —— 例如上限 12 时 `01-travel-expense-policy` 两块都抽满 12，
「私车公用申请表」被挤掉，图谱对话检索按实体名就查不到它。

做法：复用 `services/indexing.load_all_published_documents` 取全部已发布文档快照，
逐篇 `graph_build_service.build_for_document`（**先清后建，幂等**）。

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/reindex_all_kg.py [--dry-run] [--document-id ID ...]

    --dry-run 只列出将重建的文档，不写任何数据
    --document-id 只重建指定文档（可重复；缺省全量）

⚠️ 每篇每块都会调一次 LLM 抽取，有 API 成本与耗时；Neo4j 不可用时直接报错退出。
"""
import argparse
import asyncio
import logging
import sys

sys.path.insert(0, ".")

from app.db.session import async_session_factory # noqa: E402
from app.services.graph_build import graph_build_service # noqa: E402
from app.services.indexing import load_all_published_documents # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ReindexAllKg")


async def main(dry_run: bool, document_ids: list[str] | None) -> int:
    await graph_build_service.start()
    if graph_build_service._driver is None:
        logger.error("Neo4j 不可用：无法重建图谱，请先启动 neo4j 后重跑")
        return 2

    async with async_session_factory() as session:
        docs = await load_all_published_documents(session)

    if document_ids:
        wanted = {str(i) for i in document_ids}
        docs = [d for d in docs if str(d.id) in wanted]
        logger.info("按 --document-id 过滤后：%d 篇", len(docs))

    logger.info("待重建 KG 的已发布文档 %d 篇", len(docs))
    if dry_run:
        for doc in docs:
            logger.info(" - [dry-run] %s %s", doc.id, doc.title)
        await graph_build_service.close()
        return 0

    total = 0
    failed = 0
    for doc in docs:
        try:
            n = await graph_build_service.build_for_document(doc)
            total += n
            logger.info(" ✓ %s %s entities=%d", doc.id, doc.title, n)
        except Exception as err: # noqa: BLE001 —— 单篇失败不影响其余
            failed += 1
            logger.error(" ✗ %s %s：%s", doc.id, doc.title, err)

    logger.info(
        "KG 重建完成：文档=%d, 失败=%d, 实体≈%d（总耗时含 LLM 抽取）",
        len(docs),
        failed,
        total,
    )
    await graph_build_service.close()
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="全量重建 KG 知识图谱（Neo4j）")
    parser.add_argument("--dry-run", action="store_true", help="只列出，不写数据")
    parser.add_argument(
        "--document-id",
        action="append",
        default=None,
        help="只重建指定文档 id（可重复；缺省全量）",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run, args.document_id)))
