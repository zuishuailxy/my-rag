"""知识图谱 overview 接口 E2E 冒烟（真实 Neo4j，裸 FastAPI app + ASGITransport）

覆盖 GET /graph/overview（对应 TS GraphController.overview + getOverview）：
  1. 未登录 → 401
  2. 无 search 权限码的路由访问形态：docLimit 越界 → 422（Query 校验）
  3. 管理员默认查询 → 200 + 六大键结构完整（nodes/edges/stats/topEntities/recentNodes/entityTypes）
  4. keyword 命中文档标题 → 主查询路径
  5. keyword 命中实体名（标题不含）→ extra 补充路径
  6. entityType 过滤 / from 时间下界（updatedAt 字符串比较）
  7. mentions 边「提及」/ tagged 边「标注」/ 画布内 RELATED_TO
  8. 临时数据写入→查询→清理（零残留）

刻意不调 LLM：直接用 driver 写临时子图，验证真实查询路径。

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_graph_overview.py

前置：docker 的 neo4j / postgres 已就绪（连接读 backend/.env）。
"""
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from app.db.session import engine
from app.main import app
from app.services.graph_build import graph_build_service
from scripts.smoke_auth import bearer_headers

BASE = "http://testserver/api/v1"

_RID = uuid.uuid4().hex[:8]
# 文档标题关键词（主查询路径命中）
_TITLE_KW = f"SmkOvTitle{_RID}"
# 实体名关键词（extra 补充路径命中；文档标题不含）
_ENT_KW = f"SmkOvEnt{_RID}"
_TYPE = f"SmokeOvType{_RID}"
_TAG = f"SmkOvTag{_RID}"

DOC1 = f"doc1-{_RID}"          # 标题含 _TITLE_KW，同时提及 _ENT_KW 实体（画布 + extra 双路径）
DOC2 = f"doc2-{_RID}"          # 标题不含关键词，靠实体名命中 extra 路径
CHUNK1 = f"chunk1-{_RID}"
CHUNK2 = f"chunk2-{_RID}"
ENT1 = f"{_ENT_KW}-alpha"      # 与 DOC1 相连，且与 ENT2 有 RELATED_TO
ENT2 = f"{_ENT_KW}-beta"       # 仅 DOC2 的 chunk 提及
NOW = datetime.now(timezone.utc)

_results: list[str] = []


def check(step: str, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    _results.append(f"{mark} {step}: {detail}")
    if not ok:
        raise AssertionError(f"{step} 失败：{detail}")


async def _seed() -> None:
    """写入两篇文档 + 两个 chunk + 两个实体的临时子图"""
    driver = graph_build_service._driver
    assert driver is not None, "Neo4j 不可用"
    now = NOW.isoformat()
    yesterday = (NOW - timedelta(days=1)).isoformat()
    async with driver.session() as session:
        # 文档节点（updatedAt 昨天写入，便于 from 过滤断言「今天」能命中）
        for doc_id, title in ((DOC1, f"关于{_TITLE_KW}的调研报告"), (DOC2, "无关标题的文档")):
            await session.run(
                "MERGE (d:KnowledgeDocument {id: $id}) "
                "SET d.title = $title, d.summary = $summary, d.tags = $tags, "
                "    d.updatedAt = $now, d.createdAt = $yesterday",
                id=doc_id,
                title=title,
                summary=f"{title} 摘要",
                tags=f"{_TAG},其他标签",
                now=yesterday,
                yesterday=yesterday,
            )
        # chunk 节点 + HAS_CHUNK
        for chunk_id, doc_id in ((CHUNK1, DOC1), (CHUNK2, DOC2)):
            await session.run(
                "MERGE (c:DocumentChunk {chunkId: $chunkId}) "
                "SET c.documentId = $docId, c.updatedAt = $now "
                "WITH c MATCH (d:KnowledgeDocument {id: $docId}) "
                "MERGE (d)-[:HAS_CHUNK]->(c)",
                chunkId=chunk_id,
                docId=doc_id,
                now=now,
            )
        # 实体节点 + MENTIONS + RELATED_TO
        for name in (ENT1, ENT2):
            await session.run(
                "MERGE (e:KnowledgeEntity {name: $name}) "
                "SET e.type = $type, e.description = $desc",
                name=name,
                type=_TYPE,
                desc=f"{name} 描述",
            )
        await session.run(
            "MATCH (c:DocumentChunk {chunkId: $chunkId}) "
            "MATCH (e:KnowledgeEntity {name: $name}) "
            "MERGE (c)-[:MENTIONS]->(e)",
            chunkId=CHUNK1, name=ENT1,
        )
        await session.run(
            "MATCH (c:DocumentChunk {chunkId: $chunkId}) "
            "MATCH (e:KnowledgeEntity {name: $name}) "
            "MERGE (c)-[:MENTIONS]->(e)",
            chunkId=CHUNK2, name=ENT2,
        )
        await session.run(
            "MATCH (a:KnowledgeEntity {name: $a}) "
            "MATCH (b:KnowledgeEntity {name: $b}) "
            "MERGE (a)-[r:RELATED_TO]->(b) SET r.relation = '冒烟关联'",
            a=ENT1, b=ENT2,
        )


async def _clean() -> None:
    driver = graph_build_service._driver
    if driver is None:
        return
    async with driver.session() as session:
        for doc_id in (DOC1, DOC2):
            await session.run(
                "MATCH (d:KnowledgeDocument {id: $id}) "
                "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:DocumentChunk) "
                "DETACH DELETE d, c",
                id=doc_id,
            )
        for name in (ENT1, ENT2):
            await session.run(
                "MATCH (e:KnowledgeEntity {name: $name}) DETACH DELETE e", name=name
            )


def _find_doc(nodes: list[dict], doc_id: str) -> dict | None:
    return next((n for n in nodes if n.get("id") == f"doc:{doc_id}"), None)


def _find_entity(nodes: list[dict], name: str) -> dict | None:
    return next((n for n in nodes if n.get("id") == f"entity:{name}"), None)


async def main() -> None:
    print("=== 知识图谱 overview 接口冒烟 ===")
    engine.echo = False

    # 0) Neo4j 自检 + 手动初始化 driver（裸 app 不跑 lifespan）
    await graph_build_service.start()
    assert graph_build_service._driver is not None, "Neo4j 不可用，请检查 docker/neo4j"

    await _seed()
    print(f"   准备：临时子图 doc1={DOC1} / doc2={DOC2} / ent1={ENT1} / ent2={ENT2}")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1) 未登录 → 401
        r = await client.get(f"{BASE}/graph/overview")
        check("未登录", r.status_code == 401, f"期望 401，实际 {r.status_code}")

        # 2) docLimit 越界（>80）→ 422
        r = await client.get(
            f"{BASE}/graph/overview?docLimit=999", headers=bearer_headers()
        )
        check("docLimit 越界", r.status_code == 422, f"期望 422，实际 {r.status_code}")

        # 3) 默认查询 → 200 + 结构完整
        r = await client.get(f"{BASE}/graph/overview", headers=bearer_headers())
        check("管理员 overview", r.status_code == 200, f"期望 200，实际 {r.status_code}")
        body = r.json()
        for key in ("nodes", "edges", "stats", "topEntities", "recentNodes", "entityTypes"):
            check(f"响应含 {key}", key in body, f"keys={list(body.keys())}")

        # 4) keyword 命中文档标题（主查询路径）：DOC1 在画布、DOC2 不在
        r = await client.get(
            f"{BASE}/graph/overview?keyword={_TITLE_KW}", headers=bearer_headers()
        )
        check("标题关键词 200", r.status_code == 200, f"实际 {r.status_code}")
        body = r.json()
        nodes = body["nodes"]
        check(
            "主查询命中 DOC1",
            _find_doc(nodes, DOC1) is not None,
            f"doc ids={[n['id'] for n in nodes if n['kind'] == 'document']}",
        )
        check("主查询排除 DOC2", _find_doc(nodes, DOC2) is None, "DOC2 标题不含关键词")
        check(
            "DOC1 挂上实体",
            _find_entity(nodes, ENT1) is not None,
            f"entities={[n['name'] for n in nodes if n['kind'] == 'entity']}",
        )
        check(
            "实体 type 默认取图内值",
            (_find_entity(nodes, ENT1) or {}).get("type") == _TYPE,
            f"期望 {_TYPE}",
        )
        # mentions 边 + tagged 边
        edge_pairs = {(e["source"], e["target"], e["kind"]) for e in body["edges"]}
        check(
            "mentions 边",
            (f"doc:{DOC1}", f"entity:{ENT1}", "mentions") in edge_pairs,
            f"edges={sorted(edge_pairs)}",
        )
        check(
            "tagged 边",
            any(t == f"tag:{_TAG}" and k == "tagged" for _, t, k in edge_pairs),
            f"edges={sorted(edge_pairs)}",
        )
        check(
            "标签节点入画布",
            any(n.get("id") == f"tag:{_TAG}" and n.get("kind") == "tag" for n in nodes),
            f"tags={[n['name'] for n in nodes if n['kind'] == 'tag']}",
        )

        # 5) keyword 命中实体名（extra 补充路径）：两个文档都被补进来，且 related 边出现
        r = await client.get(
            f"{BASE}/graph/overview?keyword={_ENT_KW}", headers=bearer_headers()
        )
        check("实体关键词 200", r.status_code == 200, f"实际 {r.status_code}")
        body = r.json()
        nodes = body["nodes"]
        check(
            "extra 命中 DOC2（标题不含关键词）",
            _find_doc(nodes, DOC2) is not None,
            f"doc ids={[n['id'] for n in nodes if n['kind'] == 'document']}",
        )
        check(
            "related 边仅在双实体入画布时出现",
            (f"entity:{ENT1}", f"entity:{ENT2}", "related")
            in {(e["source"], e["target"], e["kind"]) for e in body["edges"]},
            "ENT1/ENT2 均在画布内，RELATED_TO 应入边表",
        )
        check(
            "stats 文档数 ≥ 2",
            body["stats"]["documentCount"] >= 2,
            f"documentCount={body['stats']['documentCount']}",
        )
        check(
            "stats.entityTypes 含临时类型",
            any(t["type"] == _TYPE for t in body["stats"]["entityTypes"]),
            f"entityTypes={body['stats']['entityTypes']}",
        )
        check(
            "topEntities 存在结构",
            all({"name", "type", "degree"} <= set(t) for t in body["topEntities"]),
            f"topEntities={body['topEntities'][:2]}",
        )
        check(
            "recentNodes 结构",
            all({"id", "name", "kind", "updatedAt"} <= set(n) for n in body["recentNodes"]),
            f"recentNodes 数={len(body['recentNodes'])}",
        )

        # 6) entityType 过滤：只保留 _TYPE 实体相连的文档
        r = await client.get(
            f"{BASE}/graph/overview?entityType={_TYPE}", headers=bearer_headers()
        )
        body = r.json()
        doc_ids = {n["id"] for n in body["nodes"] if n["kind"] == "document"}
        check(
            "entityType 过滤",
            {f"doc:{DOC1}", f"doc:{DOC2}"} <= doc_ids,
            f"doc ids={sorted(doc_ids)}",
        )

        # 7) from 时间下界：from=明天 → updatedAt（昨天）全部被滤掉
        tomorrow = (NOW + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        r = await client.get(
            f"{BASE}/graph/overview?from={tomorrow}", headers=bearer_headers()
        )
        body = r.json()
        check(
            "from 时间过滤",
            not any(n["kind"] == "document" for n in body["nodes"]),
            f"doc 节点={[n['id'] for n in body['nodes'] if n['kind'] == 'document']}",
        )
        # from=昨天 → 命中
        yesterday_str = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        r = await client.get(
            f"{BASE}/graph/overview?from={yesterday_str}", headers=bearer_headers()
        )
        body = r.json()
        check(
            "from 下界放行",
            _find_doc(body["nodes"], DOC1) is not None,
            "updatedAt=昨天 ≥ from=昨天零点 应命中",
        )

        # 8) docLimit=1 + 标题关键词：LIMIT 生效不报错
        r = await client.get(
            f"{BASE}/graph/overview?keyword={_TITLE_KW}&docLimit=1",
            headers=bearer_headers(),
        )
        check("docLimit=1", r.status_code == 200, f"实际 {r.status_code}")

    print("\n知识图谱 overview 接口冒烟全部通过 ✅")


async def run() -> None:
    """main + cleanup 同一事件循环（driver 绑定首个 loop）"""
    try:
        await main()
    except Exception:
        print("冒烟测试失败", file=sys.stderr)
        raise
    finally:
        await _clean()
        await graph_build_service.close()


if __name__ == "__main__":
    asyncio.run(run())
