"""生成侧评测：跑「问题 → 检索 → 生成回答」全链路，判分答案质量

检索评测（`eval_retrieval.py`）只测「资料有没有被找到」；本脚本补上 P0 缺的
**回答质量**指标 —— 检索全对但模型编造、乱标引用，这里才看得出来。

指标分两档：

  确定性（不花 LLM 调用，可进 CI）
    - 要点覆盖率 `fact_coverage`：答案里出现了多少期望要点
    - 引用率 `cited_rate`：多少比例的回答带了 `[n]` 引用
    - 引用越界 `bad_citation`：`[n]` 的 n 超出资料条数
    - 拒答率 `refuse_rate`：不可回答的问题里，多少条正确地说了「没有相关内容」

  LLM 判分（`--no-judge` 可关）
    - 忠实度 `faithfulness`：答案断言中被检索资料支撑的比例
    - 引用准确率 `citation_ok`：`[n]` 是否指向真正支持该句的资料

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/eval_generation.py [--limit 5] [--no-judge]

    # 把评测集推到 LangSmith（供在线实验/对比；不跑 LLM）
    .venv/bin/python scripts/eval_generation.py --push-dataset

前置：docker 栈在跑、LLM / Embedding / Rerank Key 已配（读 backend/.env）。
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, ".")

from app.db.es import es_service  # noqa: E402
from app.services.agentic_retrieve import retrieve_until_relevant  # noqa: E402
from app.services.ai_chat import ai_chat_service  # noqa: E402
from app.services.answer_grader import (  # noqa: E402
    answer_grader,
    extract_citations,
    fact_coverage,
    faithfulness_score,
)
from app.services.chat_query_rewrite import chat_query_rewrite_service  # noqa: E402
from app.services.retrieval import hybrid_retrieval_service  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
GEN_FILE = EVAL_DIR / "gen_eval.jsonl"
RETRIEVAL_FILE = EVAL_DIR / "rag_eval.jsonl"

REFUSAL = "知识库里没有相关内容。"


def load_cases(limit: int | None) -> list[dict]:
    lines = [ln for ln in GEN_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    cases = [json.loads(ln) for ln in lines]
    return cases[:limit] if limit else cases


async def answer_question(question: str, top_k: int = 5) -> dict[str, Any]:
    """评测目标：走**生产同一条**链路（检索改写 + 切题 + 生成），并保留完整上下文

    只复用 `ai_chat_service.answer_with_hits` 而不是 `chat()`：后者要落库、
    且只返回 200 字摘录 —— 判忠实度需要完整资料原文。
    """
    outcome = await retrieve_until_relevant(
        question=question,
        query=question,
        top_k=top_k,
        user=None,
        retrieval=hybrid_retrieval_service,
        rewrite=chat_query_rewrite_service,
    )
    if not outcome.hits:
        return {"answer": REFUSAL, "contexts": [], "sources": [], "hit_count": 0}

    answer, sources = await ai_chat_service.answer_with_hits(question, outcome.hits)
    return {
        "answer": answer,
        "contexts": [hit.content for hit in outcome.hits],
        "sources": sources,
        "hit_count": len(outcome.hits),
    }


async def run_case(case: dict, args: argparse.Namespace) -> dict[str, Any]:
    """跑一条用例并算该条的全部指标"""
    result = await answer_question(case["question"], args.top_k)
    answer = result["answer"]
    contexts = result["contexts"]

    citations = extract_citations(answer)
    cited = bool(citations)
    bad = [n for n in citations if n > len(contexts)]
    # 「拒答」判定：无召回，或回答就是兜底话术
    refused = result["hit_count"] == 0 or REFUSAL in answer
    coverage = fact_coverage(answer, case["expect_facts"])

    score: float | None = None
    cite_ok: bool | None = None
    unsupported: list[str] = []
    if args.judge and not refused:
        grade = await answer_grader.grade(case["question"], answer, contexts)
        if grade is not None:
            score = faithfulness_score(grade)
            cite_ok = not grade.citation_issues
            unsupported = grade.unsupported_claims

    return {
        "id": case["id"],
        "question": case["question"],
        "answerable": case["answerable"],
        "answer": answer,
        "contexts": contexts,
        "coverage": coverage,
        "cited": cited,
        "bad_citations": bad,
        "refused": refused,
        "faithfulness": score,
        "citation_ok": cite_ok,
        "unsupported_claims": unsupported,
    }


def summarize(rows: list[dict]) -> dict[str, Any]:
    """汇总指标：可回答 / 不可回答分组算，避免互相稀释"""
    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]
    judged = [r for r in rows if r["faithfulness"] is not None]

    def _avg(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    return {
        "n_answerable": len(answerable),
        "n_unanswerable": len(unanswerable),
        "fact_coverage": _avg([r["coverage"] for r in answerable]),
        "cited_rate": _avg([1.0 if r["cited"] else 0.0 for r in answerable]),
        "bad_citation_rate": _avg(
            [1.0 if r["bad_citations"] else 0.0 for r in answerable]
        ),
        "refuse_rate": _avg([1.0 if r["refused"] else 0.0 for r in unanswerable]),
        "faithfulness": _avg([r["faithfulness"] for r in judged]),
        "citation_ok_rate": _avg(
            [1.0 if r["citation_ok"] else 0.0 for r in judged if r["citation_ok"] is not None]
        ),
        "judged": len(judged),
    }


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def print_summary(metrics: dict[str, Any], judged: bool) -> None:
    print(
        f"  可回答 {metrics['n_answerable']} / 不可回答 {metrics['n_unanswerable']}"
        f"（判分覆盖 {metrics['judged']} 条）"
    )
    print(
        f"  要点覆盖率={_fmt(metrics['fact_coverage'])}  "
        f"引用率={_fmt(metrics['cited_rate'])}  "
        f"引用越界率={_fmt(metrics['bad_citation_rate'])}  "
        f"拒答率={_fmt(metrics['refuse_rate'])}"
    )
    if judged:
        print(
            f"  忠实度={_fmt(metrics['faithfulness'])}  "
            f"引用准确率={_fmt(metrics['citation_ok_rate'])}"
        )
    else:
        print("  忠实度/引用准确率：未判分（--no-judge 或未配 LLM Key）")


# ---- LangSmith 上报 ----


def _push_dataset(client: Any, name: str, path: Path) -> int:
    """把一个 jsonl 评测集推到 LangSmith（同名数据集已存在则复用）

    评测集是**单一事实来源**：LangSmith 只是它的一个运行台，
    离线脚本（可进 CI、零外部依赖）仍是主入口。
    """
    cases = [
        json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    if client.has_dataset(dataset_name=name):
        dataset = client.read_dataset(dataset_name=name)
    else:
        dataset = client.create_dataset(
            dataset_name=name, description=f"my-rag 评测集（源文件 {path.name}）"
        )

    existing = {
        ex.metadata.get("case_id")
        for ex in client.list_examples(dataset_id=dataset.id)
        if ex.metadata
    }
    fresh = [c for c in cases if c["id"] not in existing]
    if fresh:
        client.create_examples(
            dataset_id=dataset.id,
            examples=[
                {
                    "inputs": {"question": c["question"]},
                    "outputs": c,
                    "metadata": {"case_id": c["id"], "category": c.get("category")},
                }
                for c in fresh
            ],
        )
    print(f"  {name}: 共 {len(cases)} 条，新增 {len(fresh)} 条")
    return len(cases)


def push_datasets() -> int:
    """把检索集 + 生成集推到 LangSmith"""
    from langsmith import Client  # noqa: PLC0415 — 仅该模式下需要

    client = Client()
    print("推送到 LangSmith：")
    _push_dataset(client, "my-rag-retrieval-eval", RETRIEVAL_FILE)
    _push_dataset(client, "my-rag-generation-eval", GEN_FILE)
    return 0


async def run_langsmith(args: argparse.Namespace) -> int:
    """在 LangSmith 上跑一次 experiment（数据集需先 `--push-dataset`）

    目标与本地跑的是**同一个** `answer_question`，评测器复用**同一个** `answer_grader` ——
    差别只是结果落到 LangSmith，换来实验对比视图与 trace 同源。
    """
    from langsmith import Client  # noqa: PLC0415 — 仅该模式下需要
    from langsmith.evaluation import aevaluate  # noqa: PLC0415

    client = Client()

    async def target(inputs: dict) -> dict[str, Any]:
        result = await answer_question(inputs["question"], args.top_k)
        return {"answer": result["answer"], "contexts": result["contexts"]}

    async def ev_fact_coverage(run: Any, example: Any) -> dict:
        facts = (example.outputs or {}).get("expect_facts") or []
        return {
            "key": "fact_coverage",
            "score": fact_coverage(run.outputs["answer"], facts),
        }

    async def ev_refusal(run: Any, example: Any) -> dict | None:
        if (example.outputs or {}).get("answerable"):
            return None
        return {
            "key": "refuse_ok",
            "score": 1.0 if REFUSAL in run.outputs["answer"] else 0.0,
        }

    async def ev_answer_grade(run: Any, example: Any) -> list[dict] | None:
        grade = await answer_grader.grade(
            example.inputs["question"], run.outputs["answer"], run.outputs["contexts"]
        )
        if grade is None:
            return None
        return [
            {"key": "faithfulness", "score": faithfulness_score(grade)},
            {"key": "citation_ok", "score": 0.0 if grade.citation_issues else 1.0},
        ]

    results = await aevaluate(
        target,
        data="my-rag-generation-eval",
        evaluators=[ev_fact_coverage, ev_refusal, ev_answer_grade],
        experiment_prefix=args.experiment_prefix,
        max_concurrency=args.concurrency,
        client=client,
    )
    # aevaluate 返回的是 AsyncExperimentResults，wait() 是协程（不 await 会告警）
    await results.wait()
    print(f"\nLangSmith 实验：{results.experiment_name}")
    print(f"  对比视图：{results.url}")
    return 0


# ---- 主流程 ----


async def main(args: argparse.Namespace) -> int:
    if args.push_dataset:
        return push_datasets()

    await es_service.start()
    if args.langsmith:
        try:
            return await run_langsmith(args)
        finally:
            await es_service.close()

    cases = load_cases(args.limit)
    judge_on = args.judge and answer_grader.is_enabled()
    if args.judge and not judge_on:
        print("⚠️ 未配置 LLM Key，忠实度/引用准确率将跳过\n")

    rows: list[dict] = []
    for case in cases:
        row = await run_case(case, args)
        rows.append(row)
        print(
            f"  [{row['id']}] 覆盖率={row['coverage']:.2f} "
            f"引用={'有' if row['cited'] else '无'} "
            f"越界={row['bad_citations'] or '-'} "
            f"忠实度={_fmt(row['faithfulness'])}"
        )

    print(f"\n=== 生成侧评测（{GEN_FILE.name}，共 {len(rows)} 条）===")
    print_summary(summarize(rows), judge_on)

    await es_service.close()

    if args.dump:
        Path(args.dump).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n明细已写入 {args.dump}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成侧评测（忠实度 / 引用准确率）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（省 LLM 调用）")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--no-judge", dest="judge", action="store_false", help="跳过 LLM 判分")
    parser.add_argument("--push-dataset", action="store_true", help="把评测集推到 LangSmith")
    parser.add_argument("--langsmith", action="store_true", help="跑一次 LangSmith experiment")
    parser.add_argument("--experiment-prefix", default="my-rag-gen", help="实验名前缀")
    parser.add_argument("--concurrency", type=int, default=4, help="LangSmith 并发度")
    parser.add_argument("--dump", default=None, help="把逐条明细写到该 JSON 文件")
    parsed = parser.parse_args()
    raise SystemExit(asyncio.run(main(parsed)))
