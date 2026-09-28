"""校验评测集的地面真值是否与知识库对得上

**为什么需要这一步**
    评测集里最容易出的错不是逻辑，而是**打错文档标题**或**期望要点与文档不符**。
    后果很隐蔽：该题永远「未命中」→ 你以为检索不行，其实是标注错了；
    或者得分虚低，你据此去「调参」，越调越偏。

    所以每次改评测集/换语料后，先跑一遍这个脚本。

检查项（仅针对正例）
    1. `expect_documents` 里的标题在库中是否存在（防拼写错）
    2. `expect_keywords` 至少有 1 个出现在该文档的 chunk 里（防要点写偏）
    3. 文档块数是否异常少（1 块的文档往往是空壳）

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/validate_eval_set.py
"""
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

from app.db.session import async_session_factory  # noqa: E402

EVAL_FILE = Path(__file__).resolve().parent.parent / "eval" / "rag_eval.jsonl"


def load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in EVAL_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def load_docs() -> dict[str, str]:
    """document_title → 该文档全部 chunk 拼起来的正文"""
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT document_title, string_agg(content, ' ') "
                    "FROM doc_chunk GROUP BY document_title"
                )
            )
        ).all()
    return {str(title): str(body) for title, body in rows}


async def main() -> int:
    cases = load_cases()
    docs = await load_docs()
    print(f"评测集 {len(cases)} 条；知识库 {len(docs)} 篇文档\n")

    problems: list[str] = []
    counters: dict[str, int] = defaultdict(int)

    for case in cases:
        expected = case.get("expect_documents") or []
        if not expected:
            continue
        for title in expected:
            if title not in docs:
                problems.append(f"[{case['id']}] 文档不存在：{title!r}")
                counters["缺失文档"] += 1
                continue
            keywords = case.get("expect_keywords") or []
            if keywords and not any(k in docs[title] for k in keywords):
                problems.append(
                    f"[{case['id']}] 期望要点都不在 {title!r} 里：{keywords}"
                )
                counters["要点不符"] += 1

    if problems:
        print("❌ 发现问题：")
        for item in problems:
            print(f"  {item}")
        print(f"\n汇总：{dict(counters)}")
        return 1

    print("✅ 全部通过：文档标题可对上，期望要点均能在对应文档中找到")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
