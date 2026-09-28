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
# 冻结的查询向量：embedding 服务端存在浮点非确定性（同文本两次调用向量不同），
# 不冻结的话同一份评测集每次跑都会在 1~2 条题上飘。落盘后跨运行可比。
VECTOR_CACHE_FILE = Path(__file__).resolve().parent.parent / "eval" / "query_vectors.json"


def _load_vector_cache() -> int:
    """载入冻结的查询向量（不存在则跳过）"""
    import app.services.search as search_module

    if not VECTOR_CACHE_FILE.exists():
        return 0
    try:
        data = json.loads(VECTOR_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    search_module._QUERY_VECTOR_CACHE.update(data)  # type: ignore[attr-defined]
    return len(data)


def _save_vector_cache() -> int:
    """把本次用到的查询向量落盘，供下次跑复用（保证指标可复现）"""
    import app.services.search as search_module

    data = search_module._QUERY_VECTOR_CACHE  # type: ignore[attr-defined]
    try:
        VECTOR_CACHE_FILE.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as err:
        print(f"⚠️ 查询向量缓存写入失败（不影响本次指标）：{err}")
        return 0
    return len(data)


def load_cases() -> list[dict]:
    """读评测集 JSONL"""
    lines = [ln for ln in EVAL_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


class _DegradationCounter(logging.Handler):
    """统计检索链路的降级告警，用于判断本次指标是否可信

    两类降级都会**静默改变指标**，不显式提示就会被当成「参数调坏了」：
      - rerank 不可用（配额 / 网络）→ 降级 RRF，minScore 过滤失效、负例全进；
      - 查询向量化失败（欠费 / 网络）→ 只剩关键词通道，退化为纯 BM25；
        此时向量相似度兜底门控也一并失效。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.counts = {"rerank": 0, "embedding": 0}

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "RerankerService":
            self.counts["rerank"] += 1
        elif record.name == "HybridSearchService" and "向量化失败" in record.getMessage():
            self.counts["embedding"] += 1


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
    _load_vector_cache()
    counter = _DegradationCounter()
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
    _save_vector_cache()

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
            # 命中但没排第 1：recall 满分时唯一能暴露问题的信号
            "not_top1": [
                (c["id"], c["question"], titles[:3], rank)
                for c, rank, titles in rows
                if c.get("expect_documents") and rank and rank > 1
            ],
            "groups": {name: _summarize(group) for name, group in by_difficulty.items()},
            "degradations": counter.counts,
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
    degradations = metrics.get("degradations") or {}
    if degradations.get("rerank"):
        print(
            f"   ⚠️ rerank 调用失败 {degradations['rerank']} 次：已降级为 RRF，"
            "minScore 不生效、负例会漏过（本次指标不可信）"
        )
    if degradations.get("embedding"):
        print(
            f"   ⚠️ 查询向量化失败 {degradations['embedding']} 次：只剩关键词通道，"
            "向量相似度兜底也失效（本次指标不可信）"
        )


async def main(args: argparse.Namespace) -> int:
    await es_service.start()
    if args.no_rerank:
        reranker_service.enabled = False
    if args.no_vector:
        # 消融用：把查询向量化拧成「恒失败」，走 hybrid_search 自带的单路降级分支。
        # 这样「纯关键词」的指标是可重现的，不靠「正好欠费」这种意外状态。
        import app.services.search as search_module  # noqa: PLC0415

        async def _no_query_vector(query: str) -> None:  # noqa: ARG001
            return None

        search_module._embed_query = _no_query_vector  # type: ignore[assignment]

        print("已禁用向量通道（模拟 embedding 不可用）")

    service = HybridRetrievalService(
        hybrid_top_k=args.hybrid_top_k,
        rrf_c=args.rrf_c,
        min_score=args.min_score,
        min_vector_score=args.min_vector_score,
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
        print(f"\n未命中（top-k 里没有正确文档）{len(metrics['misses'])} 题：")
        for cid, question, top in metrics["misses"]:
            print(f"  ✗ [{cid}] {question}\n      检索到：{top}")
    if metrics["not_top1"]:
        print(f"\n命中但未排第 1 {len(metrics['not_top1'])} 题：")
        for cid, question, top, rank in metrics["not_top1"]:
            print(f"  · [{cid}] 正确文档在第 {rank} 位 —— {question}\n      前 3：{top}")

    await es_service.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG 检索评测（recall@k / hit@1 / MRR）")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--hybrid-top-k", type=int, default=None)
    parser.add_argument("--rrf-c", type=int, default=None)
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument(
        "--no-vector",
        action="store_true",
        help="禁用向量通道（模拟 embedding 不可用），用于消融纯关键词检索",
    )
    parser.add_argument(
        "--min-vector-score",
        type=float,
        default=None,
        help="rerank 降级时的向量相似度兜底门槛（配合 --no-rerank 扫权衡曲线）",
    )
    parsed = parser.parse_args()
    raise SystemExit(asyncio.run(main(parsed)))
