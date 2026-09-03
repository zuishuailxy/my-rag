"""知识图谱检索接口 E2E 冒烟（真实 Neo4j，裸 FastAPI app + ASGITransport）

覆盖 GraphController 全部路径：
  1. 未登录 → 401
  2. 管理员 → /graph/nodes 返回 200 + list，且 type 过滤生效
  3. 管理员 → /graph/search?keyword= 返回 200 + 命中（含 label/snippet 字段）
  4. 管理员 → /graph/edges 返回 200 + list
  5. limit 越界（>200）→ 422（Query 校验）
  6. 临时实体写入→查询→清理（验证真实 Neo4j 查询路径，零残留）

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_graph_api.py

前置：docker 的 neo4j 已就绪（连接读 backend/.env）。
"""
import asyncio
import sys
import uuid

import httpx

from app.db.session import engine
from app.main import app
from app.services.graph_build import graph_build_service
from scripts.smoke_auth import bearer_headers

BASE = "http://testserver/api/v1"

_TYPE = "SmokeGraphType"
_NAME = f"SmokeGraphEntity_{uuid.uuid4().hex[:8]}"
_DESC = f"{_NAME} 描述文本用于检索命中验证"
_KEYWORD = _NAME  # 用实体名作为关键词，稳定命中

_results: list[str] = []


def check(step: str, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    _results.append(f"{mark} {step}: {detail}")
    if not ok:
        raise AssertionError(f"{step} 失败：{detail}")


async def _seed_entity() -> None:
    """插入一个临时 KnowledgeEntity（cleanup 负责删除）"""
    driver = graph_build_service._driver
    assert driver is not None, "Neo4j 不可用"
    async with driver.session() as session:
        await session.run(
            "MERGE (e:KnowledgeEntity {name: $name}) "
            "SET e.type = $type, e.description = $desc",
            name=_NAME,
            type=_TYPE,
            desc=_DESC,
        )


async def _clean_entity() -> None:
    driver = graph_build_service._driver
    if driver is None:
        return
    async with driver.session() as session:
        await session.run(
            "MATCH (e:KnowledgeEntity {name: $name}) DETACH DELETE e",
            name=_NAME,
        )


async def main() -> None:
    print("=== 知识图谱检索接口冒烟 ===")

    # 压掉 SQLAlchemy echo（import 后设置，避免淹没断言输出）
    engine.echo = False

    # 0) Neo4j 自检 + 手动初始化 driver（裸 app 不跑 lifespan）
    await graph_build_service.start()
    assert graph_build_service._driver is not None, "Neo4j 不可用，请检查 docker/neo4j"

    # 1) 写入临时实体
    await _seed_entity()
    print(f"   准备：写入 Neo4j 临时实体 {_NAME} / type={_TYPE}")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 2) 未登录 → 401
        r = await client.get(f"{BASE}/graph/nodes")
        check("未登录", r.status_code == 401, f"期望 401，实际 {r.status_code}")

        # 3) 管理员 → nodes 200 + type 过滤命中
        r = await client.get(f"{BASE}/graph/nodes?type={_TYPE}", headers=bearer_headers())
        check("管理员 nodes", r.status_code == 200, f"期望 200，实际 {r.status_code}")
        nodes = r.json()
        check("nodes 列表", isinstance(nodes, list), f"期望 list，实际 {type(nodes)}")
        hit = next((n for n in nodes if n.get("name") == _NAME), None)
        check("nodes 过滤命中", hit is not None, f"type={_TYPE} 应命中 {_NAME}")
        if hit:
            check(
                "node 字段",
                hit.get("id") == _NAME and hit.get("type") == _TYPE,
                f"id={hit.get('id')}, type={hit.get('type')}, desc={hit.get('description')}",
            )

        # 4) 管理员 → search 命中
        r = await client.get(
            f"{BASE}/graph/search?keyword={_KEYWORD}", headers=bearer_headers()
        )
        check("管理员 search", r.status_code == 200, f"期望 200，实际 {r.status_code}")
        hits = r.json()
        check("search 列表", isinstance(hits, list), f"期望 list，实际 {type(hits)}")
        ghit = next((h for h in hits if h.get("name") == _NAME), None)
        check("search 命中", ghit is not None, f"keyword={_KEYWORD} 应命中 {_NAME}")
        if ghit:
            check(
                "search 字段",
                ghit.get("label") == "KnowledgeEntity",
                f"label={ghit.get('label')}, keys={list(ghit.keys())}",
            )

        # 5) 管理员 → edges 200 + list
        r = await client.get(f"{BASE}/graph/edges", headers=bearer_headers())
        check("管理员 edges", r.status_code == 200, f"期望 200，实际 {r.status_code}")
        edges = r.json()
        check("edges 列表", isinstance(edges, list), f"期望 list，实际 {type(edges)}")

        # 6) limit 越界（>200）→ 422（Query 校验）
        r = await client.get(
            f"{BASE}/graph/search?keyword={_KEYWORD}&limit=999", headers=bearer_headers()
        )
        check("limit 越界", r.status_code == 422, f"期望 422，实际 {r.status_code}")

    print("\n知识图谱检索接口冒烟全部通过 ✅")


async def run() -> None:
    """main + cleanup 同一事件循环（driver 绑定首个 loop）"""
    try:
        await main()
    except Exception:
        print("冒烟测试失败", file=sys.stderr)
        raise
    finally:
        await _clean_entity()
        await graph_build_service.close()


if __name__ == "__main__":
    asyncio.run(run())
