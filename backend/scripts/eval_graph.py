"""知识图谱检索评测（小评测）

**为什么单独做这一份**
    检索侧的 148 条评测集测的是「混合检索（向量 + BM25 + RRF + rerank）」，
    图谱通道完全不在里面。于是简历/口述里「图谱检索可用」这句话是**没有证据的**。
    这个脚本补上这块证据：只测**图谱通道本身**——给定的关键词能不能召回正确的
    实体与关系。

**测什么**（都是图谱通道的能力，不涉及生成）
    - 实体召回：期望实体是否出现在 `retrieve_for_chat` 返回的实体里
    - 关系召回：期望的 (source → target) 关系对是否被返回
    - **出处覆盖率**：返回的关系里带 `evidence` 的比例
      —— 这条直接验证「每条关系都能回溯到来源片段」这个宣称
    - 负例空召回：图谱里不存在的概念，应返回空而不是硬凑

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/eval_graph.py
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, ".")

from app.services.graph_build import graph_build_service  # noqa: E402

EVAL_FILE = Path(__file__).resolve().parent.parent / "eval" / "graph_eval.jsonl"


def load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in EVAL_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def run_case(case: dict, limit: int) -> dict[str, Any]:
    """跑一条，返回该条的实际命中情况"""
    hit = await graph_build_service.retrieve_for_chat(
        case["query"], limit=limit, scope=None  # scope=None = 不做可见性过滤，纯测通路
    )
    got_entities = {e["name"] for e in hit["entities"]}
    got_pairs = {(r["source"], r["target"]) for r in hit["relations"]}
    # 出处：evidence 非空即视为可回溯
    with_evidence = [r for r in hit["relations"] if (r.get("evidence") or "").strip()]

    expect_entities = set(case.get("expect_entities") or [])
    expect_pairs = [tuple(p) for p in (case.get("expect_relations") or [])]

    return {
        "id": case["id"],
        "kind": case["kind"],
        "query": case["query"],
        "note": case.get("note", ""),
        "got_entities": sorted(got_entities),
        "expect_entities": sorted(expect_entities),
        "found_entities": sorted(expect_entities & got_entities),
        "missing_entities": sorted(expect_entities - got_entities),
        "expect_pairs": expect_pairs,
        "found_pairs": [p for p in expect_pairs if p in got_pairs],
        "n_relations": len(hit["relations"]),
        "n_with_evidence": len(with_evidence),
        "n_documents": len(hit["documents"]),
    }


def summarize(rows: list[dict]) -> dict[str, Any]:
    entity_rows = [r for r in rows if r["kind"] == "entity"]
    rel_rows = [r for r in rows if r["kind"] == "relation"]
    neg_rows = [r for r in rows if r["kind"] == "negative"]
    pos_rows = entity_rows + rel_rows

    # 实体召回：只看「期望实体」这一侧
    all_hit = [r for r in pos_rows if not r["missing_entities"]]
    partial = [r for r in pos_rows if r["found_entities"]]
    total_expect = sum(len(r["expect_entities"]) for r in pos_rows)
    total_found = sum(len(r["found_entities"]) for r in pos_rows)

    # 关系召回：只统计有关系期望的用例
    pair_rows = [r for r in rel_rows if r["expect_pairs"]]
    pair_hit = [r for r in pair_rows if len(r["found_pairs"]) == len(r["expect_pairs"])]

    # 出处覆盖率：所有返回的关系里，带 evidence 的比例
    rel_total = sum(r["n_relations"] for r in rel_rows)
    rel_with_ev = sum(r["n_with_evidence"] for r in rel_rows)

    return {
        "n_entity": len(entity_rows),
        "n_relation": len(rel_rows),
        "n_negative": len(neg_rows),
        "entity_all_hit": len(all_hit) / max(len(pos_rows), 1),
        "entity_partial": len(partial) / max(len(pos_rows), 1),
        "entity_recall": total_found / max(total_expect, 1),
        "relation_hit": len(pair_hit) / max(len(pair_rows), 1),
        "evidence_rate": rel_with_ev / max(rel_total, 1),
        "negative_empty": (
            sum(1 for r in neg_rows if not r["got_entities"]) / max(len(neg_rows), 1)
        ),
    }


def print_metrics(metrics: dict[str, Any]) -> None:
    print(
        f"  实体召回：全部命中 {metrics['entity_all_hit']:.3f}  "
        f"部分命中 {metrics['entity_partial']:.3f}  "
        f"实体召回率 {metrics['entity_recall']:.3f}"
    )
    print(
        f"  关系召回：{metrics['relation_hit']:.3f}  "
        f"出处覆盖率 {metrics['evidence_rate']:.3f}  "
        f"负例空召回 {metrics['negative_empty']:.3f}"
    )


async def main(args: argparse.Namespace) -> int:
    await graph_build_service.start()
    if graph_build_service._driver is None:  # noqa: SLF001 — 判可用性的既有写法
        print("❌ Neo4j 不可用，无法评测图谱通道")
        return 2

    cases = load_cases()
    rows: list[dict] = []
    for case in cases:
        row = await run_case(case, args.limit)
        rows.append(row)
        mark = "✅" if not row["missing_entities"] else "❌"
        if case["kind"] == "negative":
            mark = "✅" if not row["got_entities"] else "❌"
        print(
            f"  {mark} [{row['id']}] {row['query']}  "
            f"实体 {len(row['found_entities'])}/{len(row['expect_entities'])}  "
            f"关系 {len(row['found_pairs'])}/{len(row['expect_pairs'])}  "
            f"返回实体 {len(row['got_entities'])}"
        )

    metrics = summarize(rows)
    print(
        f"\n=== 图谱检索评测（{EVAL_FILE.name}，"
        f"实体 {metrics['n_entity']} / 关系 {metrics['n_relation']} / "
        f"负例 {metrics['n_negative']}）==="
    )
    print_metrics(metrics)

    misses = [r for r in rows if r["missing_entities"]]
    if misses:
        print(f"\n未命中的实体 {len(misses)} 条：")
        for r in misses:
            print(f"  ✗ [{r['id']}] {r['query']}")
            print(f"      缺：{r['missing_entities']}")
            print(f"      实际返回：{r['got_entities'][:6]}")
    pair_misses = [r for r in rows if r["expect_pairs"] and not r["found_pairs"]]
    if pair_misses:
        print(f"\n未命中的关系 {len(pair_misses)} 条：")
        for r in pair_misses:
            print(f"  ✗ [{r['id']}] {r['query']}  期望 {r['expect_pairs']}")

    if args.dump:
        Path(args.dump).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n明细已写入 {args.dump}")

    await graph_build_service.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="知识图谱检索评测（实体 / 关系 / 出处）")
    parser.add_argument("--limit", type=int, default=8, help="图谱检索返回上限")
    parser.add_argument("--dump", default=None, help="逐条明细写到该 JSON")
    parsed = parser.parse_args()
    raise SystemExit(asyncio.run(main(parsed)))
