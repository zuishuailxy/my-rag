"""把 KG 实体名里的书名号归一（《X》→ X），并合并归一后重名的节点

背景：抽取对同一实体可能产出 `《发票管理办法》` 和 `发票管理办法` 两种写法，
而 `KnowledgeEntity` 按 name 建唯一约束 → 两个节点并存（实测）。
建图侧（`graph_build._write_extraction`）已改为**写入时归一**，但**历史节点仍带书名号**，
需跑本脚本一次性归一。

做法（确定性，**不重新抽取、不调 LLM**）：
1. 归一后同名的节点组用 APOC `mergeNodes` 合并（关系 / 别名一并合并），再改成归一名的 name；
2. 剩下「只带书名号、无同名节点」的直接改名。

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/normalize_kg_names.py

前置：Neo4j 可连；需要 APOC（本项目 neo4j 镜像自带）。
"""
import asyncio
import logging
import sys

sys.path.insert(0, ".")

from app.services.graph_build import graph_build_service # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("NormalizeKgNames")

# ⚠️ properties 必须用 'discard'（保留幸存节点的属性）。
# 用 'combine' 会把两节点的 description 合并成**列表**（实测把 GraphOverviewResponse
# 的 nodes[].description: str 校验冲爆），aliases 也可能被搞成字符串/嵌套列表。
_MERGE = """
MATCH (e:KnowledgeEntity)
WITH replace(replace(e.name, '《', ''), '》', '') AS norm, collect(e) AS nodes
WHERE size(nodes) > 1
CALL apoc.refactor.mergeNodes(nodes, {properties: 'discard', mergeRels: true}) YIELD node
SET node.name = norm
RETURN count(node) AS merged
"""

# 修复历史上被 'combine' 污染的类型：description 应为字符串（取第一个非空字符串项）
_REPAIR_DESC = """
MATCH (e:KnowledgeEntity)
WHERE e.description IS NOT NULL AND NOT e.description IS :: STRING
SET e.description = coalesce(
  head([x IN e.description WHERE x IS :: STRING AND x <> '']), ''
)
RETURN count(e) AS fixed
"""

# aliases 应为字符串列表：字符串包成列表，列表里过滤非字符串项
_REPAIR_ALIASES = """
MATCH (e:KnowledgeEntity)
WHERE e.aliases IS NOT NULL AND NOT e.aliases IS :: LIST<STRING>
SET e.aliases = CASE
  WHEN e.aliases IS :: STRING THEN [e.aliases]
  ELSE [x IN e.aliases WHERE x IS :: STRING]
END
RETURN count(e) AS fixed
"""

_RENAME = """
MATCH (e:KnowledgeEntity)
WHERE e.name CONTAINS '《' OR e.name CONTAINS '》'
SET e.name = replace(replace(e.name, '《', ''), '》', '')
RETURN count(e) AS renamed
"""


async def main() -> int:
    await graph_build_service.start()
    if graph_build_service._driver is None:
        logger.error("Neo4j 不可用：无法归一，请先启动 neo4j 后重跑")
        return 2

    async with graph_build_service._driver.session() as session:
        # 先合并重名组（避免改名撞唯一约束）
        merged = (await (await session.run(_MERGE)).data())[0]["merged"]
        renamed = (await (await session.run(_RENAME)).data())[0]["renamed"]
        # 修复历史 combine 污染（description 变列表 / aliases 变字符串）
        desc_fixed = (await (await session.run(_REPAIR_DESC)).data())[0]["fixed"]
        alias_fixed = (await (await session.run(_REPAIR_ALIASES)).data())[0]["fixed"]

    logger.info(
        "KG 实体名归一完成：合并=%s 组, 改名=%s 个, 修复 description=%s, aliases=%s",
        merged,
        renamed,
        desc_fixed,
        alias_fixed,
    )
    await graph_build_service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
