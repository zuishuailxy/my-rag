"""知识图谱可见性 E2E 冒烟（真实 Neo4j + Postgres；裸 FastAPI app + ASGITransport）

覆盖范围：KG 查询侧四端点按「公开 ∪ 本人 ∪ 所在团队」过滤文档节点，
以及写入侧（build_for_document 落 authorId/teamId/isPublic）与
update_visibility（改可见性立即生效）。

临时子图（直接用 driver 写，刻意不调 LLM / 不走 MQ）：

    DOC_PUB isPublic=True, authorId=admin, teamId=None → ENT_PUB
    DOC_OWN isPublic=False, authorId=user003, teamId=None → ENT_OWN
    DOC_TEAM isPublic=False, authorId=admin, teamId=成员团队 → ENT_TEAM
    DOC_OTHER isPublic=False, authorId=admin, teamId=非成员团队 → ENT_OTHER

    边：ENT_PUB -[RELATED_TO]-> ENT_TEAM （user 视角两端可见 → 出边）
        ENT_PUB -[RELATED_TO]-> ENT_OTHER （user 视角一端不可见 → 不出边）

断言：
  1. 服务层 list_nodes：user 视角恰含 3 个实体、不含 ENT_OTHER；admin 视角 4 个
  2. 服务层 search_graph：user 视角不含 DOC_OTHER / ENT_OTHER
  3. 服务层 list_edges：user 视角只有双端可见的那条；admin 视角两条都在
  4. 服务层 get_overview：user 视角画布不含 DOC_OTHER；_TYPE 的 entityTypes 计数 3 vs 4
  5. 写入侧属性落盘：DOC_TEAM 的 teamId / isPublic 值正确（_opt_str 归一为字符串）
  6. update_visibility：DOC_TEAM 改到非成员团队后，user 视角立即不可见（admin 仍可见）
  7. HTTP 端点：user / admin 两个 token 的 /graph/nodes、/graph/search 口径一致
     （验证 access_from_user + teamIds 注入的真实接线）
  8. 零残留：文档 / 块 / 实体节点全部删除，计数为 0

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_graph_visibility.py

前置：docker 的 neo4j / postgres 已就绪（连接读 backend/.env）。
"""
import asyncio
import sys
import uuid
from typing import Any

import httpx
from sqlalchemy import text

from app.common.document_access import DocumentAccessScope
from app.db.session import async_session_factory, engine
from app.main import app
from app.pipeline.types import ExtractionResult, PipelineDocument
from app.services.graph_build import _now_iso, graph_build_service
from app.services.kg_extraction import extraction_service
from app.services.team import team_service
from scripts.smoke_auth import (
    SEED_ADMIN_ID,
    SEED_ADMIN_USERNAME,
    SEED_USER_ID,
    SEED_USER_USERNAME,
    bearer_headers,
)

BASE = "http://testserver/api/v1"

_TAG = f"SmkKgVis{uuid.uuid4().hex[:8]}"
_TYPE = f"SmokeKgVisType{_TAG}"

DOC_PUB = f"kgvis-pub-{_TAG}"
DOC_OWN = f"kgvis-own-{_TAG}"
DOC_TEAM = f"kgvis-team-{_TAG}"
DOC_OTHER = f"kgvis-other-{_TAG}"
# 走真实写入链路（build_for_document）的文档，标题带独立关键词便于单独断言
DOC_BUILD = f"kgvis-build-{_TAG}"
BUILD_KW = f"{_TAG}build"

CHUNK_PUB = f"chunk-pub-{_TAG}"
CHUNK_OWN = f"chunk-own-{_TAG}"
CHUNK_TEAM = f"chunk-team-{_TAG}"
CHUNK_OTHER = f"chunk-other-{_TAG}"

ENT_PUB = f"{_TAG}-pub"
ENT_OWN = f"{_TAG}-own"
ENT_TEAM = f"{_TAG}-team"
ENT_OTHER = f"{_TAG}-other"

ALL_DOCS = (DOC_PUB, DOC_OWN, DOC_TEAM, DOC_OTHER, DOC_BUILD)
ALL_ENTS = (ENT_PUB, ENT_OWN, ENT_TEAM, ENT_OTHER)

results: list[str] = []


def check(step: str, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    results.append(f"{mark} {step}: {detail}")
    if not ok:
        raise AssertionError(f"{step} 失败：{detail}")


# --------------------------------------------------------------------- 子图读写


async def _seed(member_team: str, other_team: str) -> None:
    """写入 4 篇文档 + 4 个块 + 4 个实体 + 2 条 RELATED_TO 的临时子图"""
    driver = graph_build_service._driver
    assert driver is not None, "Neo4j 不可用"
    now = _now_iso()
    docs = (
        (DOC_PUB, True, SEED_ADMIN_ID, None),
        (DOC_OWN, False, SEED_USER_ID, None),
        (DOC_TEAM, False, SEED_ADMIN_ID, member_team),
        (DOC_OTHER, False, SEED_ADMIN_ID, other_team),
    )
    async with driver.session() as session:
        for doc_id, is_public, author_id, team_id in docs:
            await session.run(
                "MERGE (d:KnowledgeDocument {id: $id}) "
                "SET d.title = $title, d.summary = $summary, d.tags = '', "
                " d.authorId = $authorId, d.teamId = $teamId, d.isPublic = $isPublic, "
                " d.updatedAt = $now, d.createdAt = $now",
                id=doc_id,
                title=f"{_TAG} 文档 {doc_id}",
                summary=f"{_TAG} 摘要",
                authorId=author_id,
                teamId=team_id,
                isPublic=is_public,
                now=now,
            )
        pairs = (
            (CHUNK_PUB, DOC_PUB, ENT_PUB),
            (CHUNK_OWN, DOC_OWN, ENT_OWN),
            (CHUNK_TEAM, DOC_TEAM, ENT_TEAM),
            (CHUNK_OTHER, DOC_OTHER, ENT_OTHER),
        )
        for chunk_id, doc_id, ent_name in pairs:
            # 块正文刻意不带 _TAG，避免 search_graph 命中块节点干扰断言
            await session.run(
                "MERGE (c:DocumentChunk {chunkId: $chunkId}) "
                "SET c.documentId = $docId, c.content = '冒烟正文', c.updatedAt = $now "
                "WITH c MATCH (d:KnowledgeDocument {id: $docId}) "
                "MERGE (d)-[:HAS_CHUNK]->(c) "
                "WITH c MERGE (e:KnowledgeEntity {name: $name}) "
                "SET e.type = $type, e.description = '可见性冒烟实体' "
                "MERGE (c)-[:MENTIONS]->(e)",
                chunkId=chunk_id,
                docId=doc_id,
                name=ent_name,
                type=_TYPE,
                now=now,
            )
        # 双端可见的边
        await session.run(
            "MATCH (a:KnowledgeEntity {name: $a}) MATCH (b:KnowledgeEntity {name: $b}) "
            "MERGE (a)-[r:RELATED_TO]->(b) SET r.relation = '冒烟关联'",
            a=ENT_PUB,
            b=ENT_TEAM,
        )
        # 一端不可见的边（user 视角应被整条过滤掉）
        await session.run(
            "MATCH (a:KnowledgeEntity {name: $a}) MATCH (b:KnowledgeEntity {name: $b}) "
            "MERGE (a)-[r:RELATED_TO]->(b) SET r.relation = '冒烟关联'",
            a=ENT_PUB,
            b=ENT_OTHER,
        )


async def _clean() -> None:
    driver = graph_build_service._driver
    if driver is None:
        return
    async with driver.session() as session:
        for doc_id in ALL_DOCS:
            await session.run(
                "MATCH (d:KnowledgeDocument {id: $id}) "
                "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:DocumentChunk) "
                "DETACH DELETE d, c",
                id=doc_id,
            )
        for name in ALL_ENTS:
            await session.run(
                "MATCH (e:KnowledgeEntity {name: $name}) DETACH DELETE e", name=name
            )


async def _residue() -> dict[str, int]:
    """残留检查：临时文档 / 块 / 实体节点计数（应全为 0）

    块按 `c.documentId IN $docs` 统计（而非 chunkId 列表）——
    build_for_document 走真实分块链路，chunkId 由分块器生成、测试侧拿不到。
    """
    driver = graph_build_service._driver
    assert driver is not None
    async with driver.session() as session:
        record = (
            await (
                await session.run(
                    """
                    OPTIONAL MATCH (d:KnowledgeDocument)
                    WHERE d.id IN $docs
                    WITH count(d) AS docs
                    OPTIONAL MATCH (c:DocumentChunk)
                    WHERE c.documentId IN $docs
                    WITH docs, count(c) AS chunks
                    OPTIONAL MATCH (e:KnowledgeEntity)
                    WHERE e.name IN $ents
                    RETURN docs, chunks, count(e) AS ents
                    """,
                    docs=list(ALL_DOCS),
                    ents=list(ALL_ENTS),
                )
            ).single()
        )
    return {
        "docs": int(record["docs"]) if record else -1,
        "chunks": int(record["chunks"]) if record else -1,
        "ents": int(record["ents"]) if record else -1,
    }


# ------------------------------------------------------------------- 视图工具


async def _node_names(scope: DocumentAccessScope | None) -> set[str]:
    rows = await graph_build_service.list_nodes(_TYPE, 500, scope)
    return {str(r["name"]) for r in rows}


async def _search_ids(scope: DocumentAccessScope | None) -> set[str]:
    rows = await graph_build_service.search_graph(_TAG, 200, scope)
    return {str(r["id"]) for r in rows}


def _edge_pairs(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(str(r["source"]), str(r["target"])) for r in rows}


async def main() -> None:
    print("=== 知识图谱可见性 E2E 冒烟 ===")
    engine.echo = False

    await graph_build_service.start()
    assert graph_build_service._driver is not None, "Neo4j 不可用，请检查 docker/neo4j"

    # 0) 团队关系：user(003) 所在团队 + 一个他不在的团队
    async with async_session_factory() as session:
        my_team_ids = await team_service.list_user_team_ids(session, SEED_USER_ID)
        teams = (await session.execute(text("SELECT id FROM kh_team"))).scalars().all()
    all_team_ids = [str(t) for t in teams]
    assert my_team_ids, f"种子用户 {SEED_USER_ID} 无团队，冒烟前提不成立"
    member_team = my_team_ids[0]
    others = [t for t in all_team_ids if t not in my_team_ids]
    assert others, "库里只有一个团队，无法构造「非成员团队」用例"
    other_team = others[0]
    print(
        f" user(003) 团队={my_team_ids}，成员团队={member_team}，非成员团队={other_team}"
    )

    # 三种视角：普通用户（受限于三条可见路径）/ 管理员（不受限）/ 匿名（仅公开）
    user_scope = DocumentAccessScope(
        unrestricted=False, user_id=SEED_USER_ID, team_ids=list(my_team_ids)
    )
    admin_scope = DocumentAccessScope(
        unrestricted=True, user_id=SEED_ADMIN_ID, team_ids=[]
    )
    anon_scope = DocumentAccessScope(unrestricted=False, user_id="", team_ids=[])

    try:
        await _seed(member_team, other_team)
        print(f" 准备：临时子图 docs={len(ALL_DOCS) - 1} chunks=4")

        # 1) 写入侧属性落盘（teamId / isPublic 必须实际写进节点）
        driver = graph_build_service._driver
        async with driver.session() as session:
            row = (
                await (
                    await session.run(
                        "MATCH (d:KnowledgeDocument {id: $id}) "
                        "RETURN d.teamId AS teamId, d.isPublic AS isPublic, "
                        " d.authorId AS authorId",
                        id=DOC_TEAM,
                    )
                ).single()
            )
        check(
            "写入侧 teamId/isPublic 落盘",
            row is not None
            and row["teamId"] == member_team
            and row["isPublic"] is False
            and row["authorId"] == SEED_ADMIN_ID,
            f"teamId={row['teamId']!r}, isPublic={row['isPublic']!r}, "
            f"authorId={row['authorId']!r}（teamId 必须是字符串，否则 IN 过滤静默失效）",
        )

        # 2) list_nodes：三条可见路径 ∪，非成员团队不可见
        user_nodes = await _node_names(user_scope)
        admin_nodes = await _node_names(admin_scope)
        anon_nodes = await _node_names(anon_scope)
        check(
            "list_nodes(user) 公开∪本人∪团队",
            {ENT_PUB, ENT_OWN, ENT_TEAM} <= user_nodes and ENT_OTHER not in user_nodes,
            f"命中={sorted(n for n in user_nodes if n.startswith(_TAG))}",
        )
        check(
            "list_nodes(admin) 不受限",
            {ENT_PUB, ENT_OWN, ENT_TEAM, ENT_OTHER} <= admin_nodes,
            f"命中数={len(admin_nodes)}",
        )
        check(
            "list_nodes(匿名) 仅公开",
            ENT_PUB in anon_nodes and not (anon_nodes & {ENT_OWN, ENT_TEAM, ENT_OTHER}),
            f"命中={sorted(n for n in anon_nodes if n.startswith(_TAG))}",
        )

        # 3) search_graph：不可见文档及其实体都不出现
        user_hits = await _search_ids(user_scope)
        admin_hits = await _search_ids(admin_scope)
        check(
            "search_graph(user) 排除不可见文档",
            DOC_OTHER not in user_hits and ENT_OTHER not in user_hits,
            f"命中文档={sorted(h for h in user_hits if str(h).startswith('kgvis-'))}",
        )
        check(
            "search_graph(user) 保留可见文档",
            {DOC_PUB, DOC_OWN, DOC_TEAM, ENT_PUB, ENT_OWN, ENT_TEAM} <= user_hits,
            f"缺失={sorted({DOC_PUB, DOC_OWN, DOC_TEAM} - user_hits)}",
        )
        check(
            "search_graph(admin) 不受限",
            DOC_OTHER in admin_hits and ENT_OTHER in admin_hits,
            f"admin 命中数={len(admin_hits)}",
        )

        # 4) list_edges：两端都可见才出边
        user_edges = _edge_pairs(await graph_build_service.list_edges(1000, user_scope))
        admin_edges = _edge_pairs(
            await graph_build_service.list_edges(1000, admin_scope)
        )
        check(
            "list_edges(user) 双端可见才出边",
            (ENT_PUB, ENT_TEAM) in user_edges
            and (ENT_PUB, ENT_OTHER) not in user_edges,
            f"user 相关边={sorted(e for e in user_edges if ENT_PUB in e)}",
        )
        check(
            "list_edges(admin) 不受限",
            (ENT_PUB, ENT_OTHER) in admin_edges,
            f"admin 相关边={sorted(e for e in admin_edges if ENT_PUB in e)}",
        )

        # 5) get_overview：画布与统计口径都受限
        user_ov = await graph_build_service.get_overview(_TAG, None, None, None, 80, user_scope)
        admin_ov = await graph_build_service.get_overview(_TAG, None, None, None, 80, admin_scope)
        user_doc_ids = {n["id"] for n in user_ov["nodes"] if n["kind"] == "document"}
        admin_doc_ids = {n["id"] for n in admin_ov["nodes"] if n["kind"] == "document"}
        check(
            "get_overview(user) 画布排除不可见文档",
            f"doc:{DOC_OTHER}" not in user_doc_ids
            and {f"doc:{DOC_PUB}", f"doc:{DOC_OWN}", f"doc:{DOC_TEAM}"} <= user_doc_ids,
            f"user 画布文档={sorted(user_doc_ids)}",
        )
        check(
            "get_overview(admin) 画布含全部",
            f"doc:{DOC_OTHER}" in admin_doc_ids,
            f"admin 画布文档数={len(admin_doc_ids)}",
        )

        def _type_count(body: dict[str, Any]) -> int:
            return next(
                (t["count"] for t in body["stats"]["entityTypes"] if t["type"] == _TYPE),
                0,
            )

        check(
            "get_overview 统计口径受限",
            _type_count(user_ov) == 3 and _type_count(admin_ov) == 4,
            f"user={_type_count(user_ov)}, admin={_type_count(admin_ov)}（_TYPE 实体数）",
        )
        check(
            "get_overview documentCount 受限",
            admin_ov["stats"]["documentCount"] > user_ov["stats"]["documentCount"],
            f"user={user_ov['stats']['documentCount']}, "
            f"admin={admin_ov['stats']['documentCount']}",
        )
        check(
            "get_overview relatedCount 受限",
            admin_ov["stats"]["relatedCount"] > user_ov["stats"]["relatedCount"],
            f"user={user_ov['stats']['relatedCount']}, "
            f"admin={admin_ov['stats']['relatedCount']}",
        )

        # 6) HTTP 接线：user / admin 两个真实 token
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            user_headers = bearer_headers(SEED_USER_ID, SEED_USER_USERNAME)
            admin_headers = bearer_headers(SEED_ADMIN_ID, SEED_ADMIN_USERNAME)

            r = await client.get(f"{BASE}/graph/nodes", headers=user_headers)
            check("HTTP /graph/nodes(user) 200", r.status_code == 200, f"{r.status_code}")
            http_user = {n["id"] for n in r.json()}
            check(
                "HTTP /graph/nodes(user) 过滤生效",
                ENT_OTHER not in http_user and ENT_PUB in http_user,
                f"命中={sorted(n for n in http_user if str(n).startswith(_TAG))}",
            )

            r = await client.get(f"{BASE}/graph/nodes", headers=admin_headers)
            http_admin = {n["id"] for n in r.json()}
            check(
                "HTTP /graph/nodes(admin) 不受限",
                ENT_OTHER in http_admin,
                f"命中数={len(http_admin)}",
            )

            r = await client.get(
                f"{BASE}/graph/search?keyword={_TAG}", headers=user_headers
            )
            check("HTTP /graph/search(user) 200", r.status_code == 200, f"{r.status_code}")
            http_hits = {h["id"] for h in r.json()}
            check(
                "HTTP /graph/search(user) 排除不可见文档",
                DOC_OTHER not in http_hits and DOC_PUB in http_hits,
                f"命中文档={sorted(h for h in http_hits if str(h).startswith('kgvis-'))}",
            )

            r = await client.get(f"{BASE}/graph/overview?keyword={_TAG}", headers=user_headers)
            check("HTTP /graph/overview(user) 200", r.status_code == 200, f"{r.status_code}")
            ov_hits = {n["id"] for n in r.json()["nodes"] if n["kind"] == "document"}
            check(
                "HTTP /graph/overview(user) 画布受限",
                f"doc:{DOC_OTHER}" not in ov_hits,
                f"画布文档={sorted(ov_hits)}",
            )

            r = await client.get(f"{BASE}/graph/edges", headers=user_headers)
            check("HTTP /graph/edges(user) 200", r.status_code == 200, f"{r.status_code}")
            http_edges = {(e["source"], e["target"]) for e in r.json()}
            check(
                "HTTP /graph/edges(user) 双端过滤",
                (ENT_PUB, ENT_TEAM) in http_edges
                and (ENT_PUB, ENT_OTHER) not in http_edges,
                f"user 相关边={sorted(e for e in http_edges if ENT_PUB in e)}",
            )

            # 匿名（不带 token）→ 401，确认端点未因新增依赖而放开
            r = await client.get(f"{BASE}/graph/nodes")
            check("未登录 401", r.status_code == 401, f"期望 401，实际 {r.status_code}")

        # 7) update_visibility：改到非成员团队后，user 视角立即不可见（admin 不受影响）
        # 必须放在 HTTP 之后：一旦改走，user 视角连 (ENT_PUB, ENT_TEAM) 这条
        # 双端可见边也会随之消失，前面的边断言会失去意义。
        await graph_build_service.update_visibility(
            DOC_TEAM, False, other_team, SEED_ADMIN_ID
        )
        after_nodes = await _node_names(user_scope)
        admin_after = await _node_names(admin_scope)
        check(
            "update_visibility 立即生效（user 失去可见性）",
            ENT_TEAM not in after_nodes,
            f"命中={sorted(n for n in after_nodes if n.startswith(_TAG))}",
        )
        check(
            "update_visibility 不影响 admin",
            ENT_TEAM in admin_after,
            f"命中数={len(admin_after)}",
        )

        # 8) 写入侧 build_for_document：文档节点必须落 authorId / teamId / isPublic / tags
        # 抽取器换成桩（避免冒烟调 LLM），分块 / MERGE 文档节点 / HAS_CHUNK 走真实代码。
        # 作者刻意选 admin（非 user003）且团队选非成员团队 → user 三条路径都不命中，
        # 以此证明「写入侧落盘的 teamId 真的被查询侧用于过滤」。
        build_doc = PipelineDocument(
            id=DOC_BUILD,
            title=f"{BUILD_KW} 写入链路",
            content="可见性冒烟正文。" * 200,
            status=1,
            author_id=SEED_ADMIN_ID,
            team_id=other_team,
            tags="甲类,乙类",
            is_public=False,
        )
        original_extract = extraction_service.extract

        async def _stub_extract(*args: Any, **kwargs: Any) -> ExtractionResult:
            return ExtractionResult()

        extraction_service.extract = _stub_extract # type: ignore[method-assign]
        try:
            await graph_build_service.build_for_document(build_doc)
        finally:
            extraction_service.extract = original_extract

        async with driver.session() as session:
            rec = (
                await (
                    await session.run(
                        "MATCH (d:KnowledgeDocument {id: $id}) "
                        "RETURN d.teamId AS teamId, d.isPublic AS isPublic, "
                        " d.authorId AS authorId, d.tags AS tags",
                        id=DOC_BUILD,
                    )
                ).single()
            )
            chunk_n = (
                await (
                    await session.run(
                        "MATCH (:KnowledgeDocument {id: $id})-[:HAS_CHUNK]->(c) "
                        "RETURN count(c) AS n",
                        id=DOC_BUILD,
                    )
                ).single()
            )["n"]
        got = (
            f"teamId={rec['teamId']!r}, isPublic={rec['isPublic']!r}, "
            f"authorId={rec['authorId']!r}, tags={rec['tags']!r}"
            if rec is not None
            else "无文档节点"
        )
        check(
            "build_for_document 可见性字段落盘",
            rec is not None
            and rec["teamId"] == other_team
            and rec["isPublic"] is False
            and rec["authorId"] == SEED_ADMIN_ID
            and rec["tags"] == "甲类,乙类",
            got,
        )
        check("build_for_document 真实建块", int(chunk_n) >= 1, f"chunks={chunk_n}")

        # 该文档团队 = 非成员团队 → user 不可见、admin 可见；tags 应出「标注」节点
        build_user_ov = await graph_build_service.get_overview(
            BUILD_KW, None, None, None, 10, user_scope
        )
        build_admin_ov = await graph_build_service.get_overview(
            BUILD_KW, None, None, None, 10, admin_scope
        )
        check(
            "build 文档对 user 不可见",
            all(n["id"] != f"doc:{DOC_BUILD}" for n in build_user_ov["nodes"]),
            f"user 画布={[n['id'] for n in build_user_ov['nodes']]}",
        )
        check(
            "build 文档对 admin 可见 + tags 入画布",
            any(n["id"] == f"doc:{DOC_BUILD}" for n in build_admin_ov["nodes"])
            and any(
                n["id"] == "tag:甲类" and n["kind"] == "tag"
                for n in build_admin_ov["nodes"]
            ),
            f"admin 画布={[n['id'] for n in build_admin_ov['nodes']]}",
        )

        print("\n知识图谱可见性 E2E 冒烟全部通过 ✅")
        for line in results:
            print(" " + line)
    finally:
        await _clean()
        residue = await _residue()
        print(f" 残留检查：{residue}")
        check(
            "零残留",
            residue == {"docs": 0, "chunks": 0, "ents": 0},
            f"{residue}",
        )
        print(" ✅ 零残留: docs=0, chunks=0, ents=0")
        await graph_build_service.close()


async def run() -> None:
    """main + cleanup 同一事件循环（driver 绑定首个 loop）"""
    try:
        await main()
    except Exception:
        print("冒烟测试失败", file=sys.stderr)
        for line in results:
            print(" " + line, file=sys.stderr)
        raise
    finally:
        try:
            await _clean()
            await graph_build_service.close()
        except Exception: # noqa: BLE001 —— 收尾失败不覆盖原始异常
            pass


if __name__ == "__main__":
    asyncio.run(run())
