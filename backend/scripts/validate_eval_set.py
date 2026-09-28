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
from pathlib import Path

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

from app.db.session import async_session_factory  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"

# (文件名, 要点字段, 说明)：两份评测集共用同一套校验逻辑
#   两份都要校验「期望文档标题存在 + 期望要点能在该文档里找到」
TARGETS = [
    ("rag_eval.jsonl", "expect_keywords", "检索侧"),
    ("gen_eval.jsonl", "expect_facts", "生成侧"),
]


def load_cases(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
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
    docs = await load_docs()
    print(f"知识库 {len(docs)} 篇文档\n")

    problems: list[str] = []
    total = 0

    for filename, fact_field, label in TARGETS:
        cases = load_cases(EVAL_DIR / filename)
        total += len(cases)
        answerable = [c for c in cases if c.get("expect_documents")]
        print(f"{label}（{filename}）：{len(cases)} 条，其中需校验 {len(answerable)} 条")
        bad_before = len(problems)
        for case in answerable:
            for title in case["expect_documents"]:
                if title not in docs:
                    problems.append(f"[{case['id']}] 文档不存在：{title!r}")
                    continue
                facts = case.get(fact_field) or []
                if facts and not any(f in docs[title] for f in facts):
                    problems.append(
                        f"[{case['id']}] {fact_field} 均不在 {title!r} 里：{facts}"
                    )
        print(f"  → {'✅ 通过' if len(problems) == bad_before else '❌ 有问题'}")

    if problems:
        print("\n❌ 发现问题：")
        for item in problems:
            print(f"  {item}")
        return 1

    print(f"\n✅ 全部通过（共 {total} 条）：文档标题可对上，期望要点均能在对应文档中找到")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
