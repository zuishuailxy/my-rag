"""Neo4j 唯一性约束 + 写入重试 冒烟验证

验证 GraphBuildService.start() 会幂等创建三个唯一性约束，以及加了约束后
并发 MERGE 同一 key 不再产生重复节点（这正是历史上同一 documentId 出现 4 份
KnowledgeDocument 的成因）。

刻意不调 LLM：直接驱动服务的写入通道（_run_write），不经过 ExtractionService，
因此不消耗 DeepSeek token。

前置条件：docker 的 postgres / mongodb / neo4j 运行中（start() 只连 Neo4j）。

用法：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/smoke_neo4j_constraints.py
"""
import asyncio
import logging

from neo4j import AsyncSession

from app.services.graph_build import CONSTRAINT_STATEMENTS, graph_build_service

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("SmokeNeo4j")

# 测试用文档 id 与实体名；脚本结束前会清干净
TEST_DOC_ID = "smoke-neo4j-constraint-doc"
TEST_ENTITY_NAME = "冒烟-并发实体"


async def _read(session: AsyncSession, cypher: str, **params: object):
    """执行读查询并返回首列结果列表"""
    result = await session.run(cypher, **params)
    return [record[0] async for record in result]


async def _scalar(session: AsyncSession, cypher: str, **params: object):
    rows = await _read(session, cypher, **params)
    return rows[0] if rows else None


async def main() -> None:
    svc = graph_build_service

    # 1) start() 应连上并建好约束
    await svc.start()
    assert svc._driver is not None, "Neo4j 未连接，检查 NEO4J_* 配置与容器状态"
    async with svc._driver.session() as session:
        names = set(await _read(session, "SHOW CONSTRAINTS YIELD name RETURN name"))
    expected = {
        "knowledge_document_id",
        "document_chunk_chunk_id",
        "knowledge_entity_name",
    }
    assert expected <= names, f"约束缺失：{expected - names}"
    print(f"1) start() 建约束 → OK（{sorted(names)}）")

    # 2) 幂等：再 start() 一次不应报错、约束数量不变
    await svc.start()
    async with svc._driver.session() as session:
        again = set(await _read(session, "SHOW CONSTRAINTS YIELD name RETURN name"))
    assert again == names, "重复 start() 后约束集合发生变化"
    print("2) 重复 start() 幂等 → OK")

    # 3) DROP 后重建：验证 CREATE 路径（而非只命中已存在）
    async with svc._driver.session() as session:
        for stmt in CONSTRAINT_STATEMENTS:
            name = stmt.split()[2]  # CREATE CONSTRAINT <name> IF NOT EXISTS ...
            await (await session.run(f"DROP CONSTRAINT {name} IF EXISTS")).consume()
        dropped = set(await _read(session, "SHOW CONSTRAINTS YIELD name RETURN name"))
    assert not (expected & dropped), f"DROP 未生效：{expected & dropped}"
    print("3a) DROP 三个约束 → OK")
    await svc.start()
    async with svc._driver.session() as session:
        rebuilt = set(await _read(session, "SHOW CONSTRAINTS YIELD name RETURN name"))
    assert expected <= rebuilt, f"重建失败：{expected - rebuilt}"
    print("3b) start() 重新创建 → OK")

    # 4) 单次写入通道：_run_write 能正常写并消费结果
    async with svc._driver.session() as session:
        await svc._run_write(
            session,
            "MERGE (d:KnowledgeDocument {id: $id}) SET d.title = $title",
            id=TEST_DOC_ID,
            title="冒烟-约束验证",
        )
        doc_count = await _scalar(
            session,
            "MATCH (d:KnowledgeDocument {id: $id}) RETURN count(d)",
            id=TEST_DOC_ID,
        )
    assert doc_count == 1, f"文档节点写入异常：count={doc_count}"
    print("4) _run_write 写入 + consume → OK")

    # 5) 并发 MERGE 同一实体名：约束生效后应只有 1 个节点（历史上会变多份）
    async def _merge_entity(index: int) -> None:
        async with svc._driver.session() as session:  # type: ignore[union-attr]
            await svc._run_write(
                session,
                "MERGE (e:KnowledgeEntity {name: $name}) "
                "ON CREATE SET e.description = $desc",
                name=TEST_ENTITY_NAME,
                desc=f"由并发任务 {index} 创建",
            )

    await asyncio.gather(*(_merge_entity(i) for i in range(5)))
    async with svc._driver.session() as session:
        entity_count = await _scalar(
            session,
            "MATCH (e:KnowledgeEntity {name: $name}) RETURN count(e)",
            name=TEST_ENTITY_NAME,
        )
    assert entity_count == 1, f"并发 MERGE 产生重复节点：count={entity_count}"
    print("5) 并发 MERGE ×5 → 仅 1 个实体节点，无重复 → OK")

    # 6) 清理：删测试文档节点（走服务自身的删除通道）+ 孤儿实体
    await svc.delete_for_document(TEST_DOC_ID)
    async with svc._driver.session() as session:
        await (await session.run(
            "MATCH (e:KnowledgeEntity {name: $name}) DETACH DELETE e",
            name=TEST_ENTITY_NAME,
        )).consume()
        nodes = await _scalar(session, "MATCH (n) RETURN count(n)")
        rels = await _scalar(session, "MATCH ()-[r]->() RETURN count(r)")
    await svc.close()

    assert nodes == 0 and rels == 0, f"清理未净：nodes={nodes}, rels={rels}"
    print(f"6) 清理 → OK（nodes={nodes}, rels={rels}）")
    print("\n全部通过：Neo4j 约束创建幂等、并发 MERGE 无重复、写入通道正常")


if __name__ == "__main__":
    asyncio.run(main())
