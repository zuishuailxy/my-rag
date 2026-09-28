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
from app.services.embedding import embedding_provider  # noqa: E402
from app.services.indexing import (  # noqa: E402
    load_all_published_documents,
    reindex_document,
)
from sqlalchemy import text  # noqa: E402
from app.services.retrieval import HybridRetrievalService  # noqa: E402
from scripts.eval_retrieval import evaluate, load_cases, print_metrics  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")


async def _chunk_count() -> int:
    """当前库里的块总数（用于基线比对）"""
    async with async_session_factory() as session:
        rows = await session.execute(text("SELECT count(*) FROM doc_chunk"))
    return int(rows.scalar() or 0)


async def _restore_with_retry(size: int, overlap: int, attempts: int = 3) -> int:
    """恢复原尺寸；**每次尝试用全新会话**

    教训：PG 在资源紧张时会崩（进入 crash recovery），而挂在 `finally` 里的恢复
    如果复用同一个已失效的会话，会跟着一起失败 —— 结果索引被留在扫描到一半的尺寸。
    所以：开新会话 + 重试 + 事后比对基线。
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with async_session_factory() as session:
                docs = await load_all_published_documents(session)
                return await _rebuild(session, docs, size, overlap)
        except Exception as err:  # noqa: BLE001
            last_error = err
            print(f"  ⚠️ 恢复第 {attempt}/{attempts} 次失败：{str(err)[:120]}")
            await asyncio.sleep(10)
    raise SystemExit(
        f"❌ 恢复失败（{last_error}）—— 请手动跑："
        "PYTHONPATH=. python scripts/reindex_all_chunks.py"
    )


async def _assert_embedding_available() -> None:
    """开跑前先验证 embedding 可用

    重建索引必需嵌入（欠费 / 网络问题会中途失败）。虽然“先嵌入后删旧”的顺序保证了
    单篇失败不会造成空窗，但多篇重建到一半失败会留下**混合尺寸**的索引；
    而此时用于恢复的 finally 同样依赖嵌入，可能一起失败。所以宁可开跑前就崩。
    """
    try:
        await embedding_provider.embed_texts(["preflight"])
    except Exception as err:  # noqa: BLE001
        raise SystemExit(f"embedding 不可用，已中止（未改动任何索引）：{err}") from err


async def _rebuild(session: object, docs: list, size: int, overlap: int) -> int:
    """按指定尺寸重配分块器并重建全部文档索引，返回块总数"""
    chunking_service.configure(size, overlap)
    total = 0
    for doc in docs:
        total += await reindex_document(session, doc)  # type: ignore[arg-type]
    return total


async def main(args: argparse.Namespace) -> int:
    await _assert_embedding_available()
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
        baseline = int(
            (
                await session.execute(text("SELECT count(*) FROM doc_chunk"))
            ).scalar()
            or 0
        )
        print(f"基线块数 = {baseline}（恢复后校对这个数）")
        results: list[tuple[int, int, int, dict]] = []
        try:
            for size in sizes:
                overlap = max(1, round(size * args.overlap_ratio))
                try:
                    total = await _rebuild(session, docs, size, overlap)
                except Exception as err:  # noqa: BLE001
                    print(f"  ⚠️ chunk={size} 重建失败，跳过：{str(err)[:120]}")
                    continue
                metrics = await evaluate(HybridRetrievalService(), cases, args.top_k)
                results.append((size, overlap, total, metrics))
                print_metrics(
                    f"[chunk={size}/overlap={overlap}, 块数={total}]", metrics, args.top_k
                )
        finally:
            total = await _restore_with_retry(original_size, original_overlap)
            print(
                f"已恢复原尺寸 chunk={original_size}/overlap={original_overlap}"
                f"（块数={total}）"
            )

    restored = await _chunk_count()
    if restored == baseline:
        print(f"✅ 恢复校验通过：块数回到基线 {baseline}")
    else:
        print(
            f"⚠️ 恢复校验不一致：当前 {restored} ≠ 基线 {baseline}；"
            "请跑 PYTHONPATH=. python scripts/reindex_all_chunks.py 重建"
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
