"""清空 Neo4j 全部节点与关系（保留约束），一次性维护脚本。

用法：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/neo4j_wipe_data.py
"""
import asyncio

from neo4j import AsyncGraphDatabase

URI = "bolt://localhost:7687"
AUTH = ("neo4j", "12345678")


async def main() -> None:
    driver = AsyncGraphDatabase.driver(URI, auth=AUTH)
    try:
        async with driver.session() as session:
            before = (await (await session.run("MATCH (n) RETURN count(n) AS c")).single())["c"]
            print(f"删除前节点数: {before}")
            await (await session.run("MATCH (n) DETACH DELETE n")).consume()
            after = (await (await session.run("MATCH (n) RETURN count(n) AS c")).single())["c"]
            print(f"删除后节点数: {after}")
            result = await session.run("SHOW CONSTRAINTS YIELD name RETURN name")
            names = [r["name"] async for r in result]
            print(f"保留的约束: {names}")
            assert after == 0, "清空失败"
            assert len(names) == 3, "约束数量异常"
    finally:
        await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
