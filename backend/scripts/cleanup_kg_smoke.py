"""清理知识图谱里的冒烟测试残留

**为什么需要**
    跑 `smoke_graph_visibility.py` 之类的脚本会在 Neo4j 里留下以
    `SmkKgVis<hash>` 命名的文档与实体。它们会：
      - 让「图谱覆盖了多少文档」这个数字虚高（实测一次残留了 8 篇文档，
        把真实覆盖数从 6 吹成 14）；
      - 污染实体/关系统计（5 个脏实体、4 条牵连关系、2 条「冒烟关联」关系）；
      - 在评测/演示里被当成真实数据。

**删除范围**（只匹配冒烟特征，绝不碰真实数据）
    1. `KnowledgeDocument.title` 以 `SmkKgVis` 开头
    2. `KnowledgeEntity` 的 `type` 以 `Smoke` 开头，或 `name` 含 `SmkKgVis`
    3. 删除上述文档后产生的**孤儿 chunk**（不再属于任何文档）

用法：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/cleanup_kg_smoke.py --dry-run
    cd backend && PYTHONPATH=. .venv/bin/python scripts/cleanup_kg_smoke.py
"""
import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from app.services.graph_build import graph_build_service  # noqa: E402

# 冒烟测试的命名特征（脚本里统一用这个前缀）
SMOKE_PREFIX = "SmkKgVis"

_SMOKE_DOCS = (
    "MATCH (d:KnowledgeDocument) WHERE d.title STARTS WITH $prefix RETURN count(d)"
)
_SMOKE_ENTITIES = (
    "MATCH (e:KnowledgeEntity) "
    "WHERE e.type STARTS WITH 'Smoke' OR e.name CONTAINS $prefix "
    "RETURN count(e)"
)
_ORPHAN_CHUNKS = (
    "MATCH (c:DocumentChunk) WHERE NOT (c)<-[:HAS_CHUNK]-() RETURN count(c)"
)


async def _count(session, query: str) -> int:
    async for row in await session.run(query, prefix=SMOKE_PREFIX):
        return int(list(row.values())[0])
    return 0


async def main(dry_run: bool) -> int:
    await graph_build_service.start()
    driver = graph_build_service._driver  # noqa: SLF001 — 脚本内直连，沿用既有写法
    if driver is None:
        print("❌ Neo4j 不可用")
        return 2

    async with driver.session() as session:
        n_docs = await _count(session, _SMOKE_DOCS)
        n_ents = await _count(session, _SMOKE_ENTITIES)
        n_orphans = await _count(session, _ORPHAN_CHUNKS)
        # 删文档后会变成孤儿的 chunk（挂着冒烟文档的那些），dry-run 也要能看到
        n_doomed_chunks = await _count(
            session,
            "MATCH (d:KnowledgeDocument)-[:HAS_CHUNK]->(c:DocumentChunk) "
            "WHERE d.title STARTS WITH $prefix RETURN count(c)",
        )
        total_docs = await _count(
            session, "MATCH (d:KnowledgeDocument) RETURN count(d)"
        )
        total_ents = await _count(
            session, "MATCH (e:KnowledgeEntity) RETURN count(e)"
        )

        print("待清理：")
        print(f"  冒烟文档   {n_docs} 篇 / 现有 {total_docs} 篇")
        print(f"  冒烟实体   {n_ents} 个 / 现有 {total_ents} 个")
        print(f"  孤儿 chunk {n_orphans} 个")
        print(f"  删文档后会跟着清掉的 chunk {n_doomed_chunks} 个")

        if n_docs:
            print("\n  将删除的文档：")
            async for row in await session.run(
                "MATCH (d:KnowledgeDocument) WHERE d.title STARTS WITH $prefix "
                "RETURN d.title AS t ORDER BY t",
                prefix=SMOKE_PREFIX,
            ):
                print(f"    {row['t']}")

        if dry_run:
            print("\n（dry-run，未做任何改动）")
            await graph_build_service.close()
            return 0

        if not (n_docs or n_ents or n_orphans):
            print("\n✅ 没有需要清理的残留")
            await graph_build_service.close()
            return 0

        # 先删文档与实体（DETACH 会连带删掉它们的 HAS_CHUNK / MENTIONS / RELATED_TO 边）
        await session.run(
            "MATCH (d:KnowledgeDocument) WHERE d.title STARTS WITH $prefix "
            "DETACH DELETE d",
            prefix=SMOKE_PREFIX,
        )
        await session.run(
            "MATCH (e:KnowledgeEntity) "
            "WHERE e.type STARTS WITH 'Smoke' OR e.name CONTAINS $prefix "
            "DETACH DELETE e",
            prefix=SMOKE_PREFIX,
        )
        # 再清孤儿 chunk（上一步删文档后，属于它们的 chunk 已无人引用）
        await session.run(
            "MATCH (c:DocumentChunk) WHERE NOT (c)<-[:HAS_CHUNK]-() DETACH DELETE c"
        )

        left_docs = await _count(session, "MATCH (d:KnowledgeDocument) RETURN count(d)")
        left_ents = await _count(session, "MATCH (e:KnowledgeEntity) RETURN count(e)")
        left_chunks = await _count(
            session, "MATCH (c:DocumentChunk) RETURN count(c)"
        )
        left_smoke = await _count(session, _SMOKE_DOCS) + await _count(
            session, _SMOKE_ENTITIES
        )

        print("\n清理后：")
        print(f"  文档 {left_docs} 篇 · 实体 {left_ents} 个 · chunk {left_chunks} 个")
        print(f"  残留冒烟节点：{left_smoke}（应为 0）")

    await graph_build_service.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="清理知识图谱里的冒烟测试残留")
    parser.add_argument("--dry-run", action="store_true", help="只列出待清理项，不删除")
    parsed = parser.parse_args()
    raise SystemExit(asyncio.run(main(parsed.dry_run)))
