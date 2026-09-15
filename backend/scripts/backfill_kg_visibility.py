"""KG 文档节点可见性字段回填（存量数据；不重新分块、不调 LLM，零 token）

背景：GraphBuildService.build_for_document 自「KG 可见性」这轮起才写
authorId / teamId / isPublic 三个属性。此前建好的 KnowledgeDocument 节点缺这三个值，
neo4j_document_access_where 的四条判据（作者 / 公开 / 团队 + 不受限短路）全部不成立，
结果是这些文档及其提及的实体、相关的 RELATED_TO 边，**除 ADMIN / REVIEWER 外
全图不可见**，且不报错（`null IN [...]` 与 `null = 'x'` 都只是不匹配，不是错误）。

修复方式刻意不重建图谱：可见性只是文档节点上的三个属性，重跑抽取（LLM）没有意义。
本脚本按 PG 的 kh_document 逐篇调用 GraphBuildService.update_visibility ——
与「已发布文档改可见性」链路**同一份代码**，避免回填另写一套导致漂移。

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/backfill_kg_visibility.py [--dry-run]

前置：docker 的 neo4j / postgres 已就绪（连接读 backend/.env）。
幂等：重复执行结果一致；不在 Neo4j 里的文档（未建图）会被跳过，只记 matched=False。
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from app.db.session import async_session_factory, engine
from app.models.document import DocumentEntity
from app.models.document_status import DocumentStatus
from app.services.graph_build import graph_build_service


async def _missing_count(ids: list[str]) -> int:
    """统计「节点存在但缺 isPublic 属性」的文档数（=`IS NULL` 作为未回填的标记）

    节点整个不存在（从未建图）不计入 —— 本脚本不负责建图。
    """
    driver = graph_build_service._driver
    assert driver is not None
    async with driver.session() as session:
        record = (
            await (
                await session.run(
                    """
                    MATCH (d:KnowledgeDocument)
                    WHERE d.id IN $ids AND d.isPublic IS NULL
                    RETURN count(d) AS n
                    """,
                    ids=ids,
                )
            ).single()
        )
    return int(record["n"]) if record else 0


async def _node_state(doc_id: str) -> dict | None:
    """读取单个文档节点的三个可见性属性（不存在返回 None）"""
    driver = graph_build_service._driver
    assert driver is not None
    async with driver.session() as session:
        record = (
            await (
                await session.run(
                    "MATCH (d:KnowledgeDocument {id: $id}) "
                    "RETURN d.isPublic AS isPublic, d.teamId AS teamId, "
                    "       d.authorId AS authorId",
                    id=doc_id,
                )
            ).single()
        )
    return dict(record) if record is not None else None


async def main(dry_run: bool) -> None:
    print(f"=== KG 文档节点可见性回填{'（dry-run）' if dry_run else ''} ===")
    engine.echo = False

    await graph_build_service.start()
    assert graph_build_service._driver is not None, "Neo4j 不可用，请检查 docker/neo4j"

    # 与发布链路同口径：未软删 + 已发布
    async with async_session_factory() as session:
        entities = (
            (
                await session.execute(
                    select(DocumentEntity).where(
                        DocumentEntity.deleted.is_(False),
                        DocumentEntity.status == DocumentStatus.PUBLISHED,
                    )
                )
            )
            .scalars()
            .all()
        )
    targets = [
        (str(e.id), bool(e.is_public), e.team_id, e.author_id) for e in entities
    ]
    print(f"   PG 已发布文档：{len(targets)} 篇")

    ids = [t[0] for t in targets]
    before = await _missing_count(ids) if ids else 0
    print(f"   回填前缺可见性属性的文档节点：{before} 个")

    touched = 0
    for doc_id, is_public, team_id, author_id in targets:
        state = await _node_state(doc_id)
        if state is None:
            print(f"   - {doc_id}: 图中无节点，跳过（未建图）")
            continue
        print(
            f"   - {doc_id}: {state} → "
            f"{{'isPublic': {is_public}, 'teamId': {team_id!r}, 'authorId': {author_id!r}}}"
        )
        if dry_run:
            continue
        await graph_build_service.update_visibility(
            doc_id, is_public, team_id, author_id
        )
        touched += 1

    if not dry_run:
        after = await _missing_count(ids) if ids else 0
        print(f"   回填后缺可见性属性的文档节点：{after} 个（写入 {touched} 篇）")
        assert after == 0, f"仍有 {after} 个节点缺可见性属性，回填未完成"
        print("   ✅ 回填完成：所有已发布文档的图谱节点均已带 isPublic/teamId/authorId")
    else:
        print("   （dry-run 未写入；去掉 --dry-run 执行回填）")

    await graph_build_service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KG 文档节点可见性字段回填")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印将要写入的值，不改 Neo4j"
    )
    args = parser.parse_args()
    try:
        asyncio.run(main(args.dry_run))
    except Exception:
        print("回填失败", file=sys.stderr)
        raise
