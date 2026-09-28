"""RAG 检索评测：recall@k / hit@1 / MRR（+ 负例空召回率）

读 `eval/rag_eval.jsonl`（每条：question + expect_documents），对每题跑项目真实混合检索
（ES 关键词 + PG 向量 → RRF → rerank → minScore），统计命中与否、命中排名，并列出未命中的题。

也可当作「调参实验」跑：同一批评测集只改一个参数，看指标怎么变 ——
    --top-k / --min-score / --no-rerank / --hybrid-top-k / --rrf-c

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/eval_retrieval.py [--top-k 5] [--min-score 0.1] [--no-rerank]

前置：ES / PG 在跑、Rerank Key 已配（读 backend/.env）；以匿名（仅公开文档）范围评测。
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, ".")

from app.core.config import settings  # noqa: E402
from app.db.es import es_service  # noqa: E402
from app.services.reranker import reranker_service  # noqa: E402
from app.services.retrieval import HybridRetrievalService  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

EVAL_FILE = Path(__file__).resolve().parent.parent / "eval" / "rag_eval.jsonl"


def load_cases() -> list[dict]:
    """读评测集 JSONL"""
    lines = [ln for ln in EVAL_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


class _RerankFailureCounter(logging.Handler):
    """统计 RerankerService 的 WARNING 条数，用来判断 rerank 是否真的生效

    rerank 失败（配额 / 网络）会静默降级为 RRF —— 此时 minScore 过滤不生效、
    负例全进，但指标看起来只是「变差」。不显式提示就会被误读为参数调坏了。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "RerankerService":
            self.count += 1


def _summarize(rows: list[tuple[dict, int | None, list[str]]]) -> dict[str, Any]:
    """(case, rank, titles) 列表 → 指标；rank=None 表示未命中，titles 为空表示空召回"""
    positives = [r for r in rows if r[0].get("expect_documents")]
    negatives = [r for r in rows if not r[0].get("expect_documents")]
    n = max(len(positives), 1)
    return {
        "n_positive": len(positives),
        "n_negative": len(negatives),
        "recall": sum(1 for r in positives if r[1]) / n,
        "hit1": sum(1 for r in positives if r[1] == 1) / n,
        "mrr": sum(1.0 / r[1] for r in positives if r[1]) / n,
        # 该分组没有负例时为空召回率无意义，置 None（打印成 "-"）
        "empty_rate": (
            sum(1 for r in negatives if not r[2]) / len(negatives) if negatives else None
        ),
    }


async def evaluate(
    service: HybridRetrievalService, cases: list[dict], top_k: int
) -> dict[str, Any]:
    """跑检索并算指标（供单次评测与 chunk size 扫描复用）

    额外按 `difficulty` 字段分组（缺省 easy），用于把「简单题满分」和
    「难题才能拉开区分度」分开看 —— 否则混合平均会被简单题淹没。
    """
    counter = _RerankFailureCounter()
    logging.getLogger().addHandler(counter)
    rows: list[tuple[dict, int | None, list[str]]] = []
    try:
        for case in cases:
            got = await service.retrieve(case["question"], top_k, None)
            titles = [g["documentTitle"] for g in got]
            expected = set(case.get("expect_documents") or [])
            rank = next((i + 1 for i, t in enumerate(titles) if t in expected), None)
            rows.append((case, rank, titles))
    finally:
        logging.getLogger().removeHandler(counter)

    by_difficulty: dict[str, list[tuple[dict, int | None, list[str]]]] = {}
    for row in rows:
        by_difficulty.setdefault(row[0].get("difficulty", "easy"), []).append(row)

    metrics = _summarize(rows)
    metrics.update(
        {
            "misses": [
                (c["id"], c["question"], titles[:3])
                for c, rank, titles in rows
                if c.get("expect_documents") and not rank
            ],
            "groups": {name: _summarize(group) for name, group in by_difficulty.items()},
            "rerank_failures": counter.count,
        }
    )
    return metrics


def print_metrics(label: str, metrics: dict[str, Any], top_k: int) -> None:
    empty = metrics["empty_rate"]
    empty_text = "-" if empty is None else f"{empty:.3f}"
    print(
        f"{label}：recall@{top_k}={metrics['recall']:.3f}  "
        f"hit@1={metrics['hit1']:.3f}  MRR={metrics['mrr']:.3f}  "
        f"负例空召回率={empty_text}"
    )
    if metrics.get("rerank_failures"):
        print(
            f"   ⚠️ rerank 调用失败 {metrics['rerank_failures']} 次：已降级为 RRF，"
            "minScore 不生效、负例会漏过（本次指标不可信）"
        )


async def main(args: argparse.Namespace) -> int:
    await es_service.start()
    if args.no_rerank:
        reranker_service.enabled = False

    service = HybridRetrievalService(
        hybrid_top_k=args.hybrid_top_k,
        rrf_c=args.rrf_c,
        min_score=args.min_score,
    )
    min_score = args.min_score if args.min_score is not None else settings.RAG_MIN_SCORE

    cases = load_cases()
    metrics = await evaluate(service, cases, args.top_k)

    print(f"\n=== RAG 检索评测（{EVAL_FILE.name}）===")
    print(
        f"参数：top_k={args.top_k} min_score={min_score} "
        f"rerank={'off' if args.no_rerank else 'on'} "
        f"hybrid_top_k={args.hybrid_top_k or settings.RAG_HYBRID_TOP_K} "
        f"rrf_c={args.rrf_c or settings.RAG_RRF_C}"
    )
    print_metrics(f"正例 {metrics['n_positive']} / 负例 {metrics['n_negative']}", metrics, args.top_k)
    for name, group in (metrics.get("groups") or {}).items():
        if len(metrics["groups"]) > 1:
            print_metrics(f"  └ {name}", group, args.top_k)
    if metrics["misses"]:
        print(f"\n未命中 {len(metrics['misses'])} 题：")
        for cid, question, top in metrics["misses"]:
            print(f"  ✗ [{cid}] {question}\n      检索到：{top}")

    await es_service.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG 检索评测（recall@k / hit@1 / MRR）")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--hybrid-top-k", type=int, default=None)
    parser.add_argument("--rrf-c", type=int, default=None)
    parser.add_argument("--no-rerank", action="store_true")
    parsed = parser.parse_args()
    raise SystemExit(asyncio.run(main(parsed)))
