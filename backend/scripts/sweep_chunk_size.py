"""chunk size 扫描（调参实验）：对每个候选值重建 RAG 索引并跑检索评测

链路上 chunk size 是最影响召回的参数之一，但它需要**重新分块 + 重新嵌入 + 重建索引**，
属于「贵档」调参。本脚本把它自动化：逐个尺寸重建 → 跑 `eval_retrieval` 指标 → 汇总表格。

⚠️ 会改写 PG doc_chunk + ES kh_chunk（不丢原文，可重建）。跑完**自动恢复原尺寸并重建**；
chunkId 由 `sha256(documentId:index)` 确定性生成，原尺寸下 ID 与扫描前一致。

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/sweep_chunk_size.py --sizes 256,512,1024 [--overlap-ratio 0.125] [--top-k 5]

前置：ES / PG 在跑、Embedding + Rerank Key 已配（读 backend/.env）。
"""
import argparse
import asyncio
import logging
import sys

sys.path.insert(0, ".")

from app.core.config import settings  # noqa: E402
from app.db.es import es_service  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.services.chunker import chunking_service  # noqa: E402
from app.services.indexing import (  # noqa: E402
    load_all_published_documents,
    reindex_document,
)
from app.services.retrieval import HybridRetrievalService  # noqa: E402
from scripts.eval_retrieval import evaluate, load_cases, print_metrics  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")


async def _rebuild(session: object, docs: list, size: int, overlap: int) -> int:
    """按指定尺寸重配分块器并重建全部文档索引，返回块总数"""
    chunking_service.configure(size, overlap)
    total = 0
    for doc in docs:
        total += await reindex_document(session, doc)  # type: ignore[arg-type]
    return total


async def main(args: argparse.Namespace) -> int:
    await es_service.start()
    cases = load_cases()
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    original_size = settings.RAG_CHUNK_SIZE
    original_overlap = settings.RAG_CHUNK_OVERLAP

    async with async_session_factory() as session:
        docs = await load_all_published_documents(session)
        print(
            f"待扫描 chunk size：{sizes}（overlap = size × {args.overlap_ratio}）；"
            f"文档 {len(docs)} 篇；原尺寸 {original_size}/{original_overlap}"
        )
        results: list[tuple[int, int, int, dict]] = []
        try:
            for size in sizes:
                overlap = max(1, round(size * args.overlap_ratio))
                total = await _rebuild(session, docs, size, overlap)
                metrics = await evaluate(HybridRetrievalService(), cases, args.top_k)
                results.append((size, overlap, total, metrics))
                print_metrics(
                    f"[chunk={size}/overlap={overlap}, 块数={total}]", metrics, args.top_k
                )
        finally:
            total = await _rebuild(session, docs, original_size, original_overlap)
            print(
                f"已恢复原尺寸 chunk={original_size}/overlap={original_overlap}"
                f"（块数={total}）"
            )

    print(f"\n=== chunk size 扫描汇总（top_k={args.top_k}）===")
    header = f"{'chunk':>6} {'overlap':>8} {'块数':>6} {'recall':>8} {'hit@1':>7} {'MRR':>7} {'空召回率':>9}"
    print(header)
    for size, overlap, total, m in results:
        print(
            f"{size:>6} {overlap:>8} {total:>6} {m['recall']:>8.3f} "
            f"{m['hit1']:>7.3f} {m['mrr']:>7.3f} {m['empty_rate']:>9.3f}"
        )

    await es_service.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="chunk size 扫描（重建索引 + 检索评测）")
    parser.add_argument("--sizes", default="256,512,1024", help="逗号分隔的 chunk size 列表")
    parser.add_argument(
        "--overlap-ratio", type=float, default=0.125, help="overlap = size × 该比例"
    )
    parser.add_argument("--top-k", type=int, default=5)
    parsed = parser.parse_args()
    raise SystemExit(asyncio.run(main(parsed)))
