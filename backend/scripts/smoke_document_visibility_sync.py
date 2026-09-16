"""已发布文档「正文 + 可见性」同时改 → 三处索引必须立即同步（需审模式回归）

背景（2026-09-16 用户实测越权）
    登录「后端开发组」的普通用户（种子 user = 1000000000000000003，ROLE_USER）
    用 POST /search 关键字搜到一篇 PG 里**已改成「私有 + 前端开发组」**的文档。
    根因：需审模式（DOCUMENT_REQUIRE_APPROVAL=true）下 PATCH **同时**改正文与可见性时，
    DocumentService._sync_pipeline_after_update 的 content_changed 分支把可见性同步
    整个 return 掉了 —— TS 的条件是
        `isPublished && contentChanged && !isRequireApproval()`
    需审时该分支本就不成立、应继续往下走；移植时把 `!isRequireApproval()`
    挪进了 if 体内，等于「需审也 return」。
    结果：PG 已是新值，而 ES kh_chunk / ES kh_document / Neo4j 三处仍是建索引那一刻的
    旧值（公开 + 原团队）→ 任何登录用户（甚至匿名，因为 isPublic=true）都能搜到
    标题与摘要。全程无日志（静默）。

断言（修复前 5/6/7/8 全红，修复后全绿）
    1) Mongo version 递增 → 证明本次确实 content_changed=True（场景成立的前提）
    2) PG 元数据已是新值（控制项）
    3) ES kh_document 的 isPublic / teamId 已刷成新值
    4) ES kh_chunk 的 is_public / team_id 已刷成新值
    5) Neo4j 文档节点的 isPublic / teamId 已刷成新值
    6) 需审模式**不得**重建索引：ES 正文仍是旧值（证明走的是「就地刷字段」而非整篇重灌）

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_document_visibility_sync.py

前置：docker 的 postgres / mongodb / elasticsearch / neo4j 已就绪（连接读 backend/.env）。
"""
import asyncio
import sys
from datetime import datetime, timezone

from bson import ObjectId
from sqlalchemy import delete as sa_delete

from app.core.config import settings
from app.db.es import es_service
from app.db.mongo import close_mongo_client, get_document_content_collection
from app.db.session import async_session_factory, engine
from app.models.document import DocumentEntity
from app.models.document_status import DocumentStatus
from app.schemas.auth import AuthUser
from app.schemas.document import DocumentUpdate
from app.services.document import DocumentService
from app.services.es_index import CHUNK_INDEX
from app.services.graph_build import graph_build_service
from app.services.search_index import DOCUMENT_INDEX, search_index_service

# 压掉 SQLAlchemy echo 日志（import 后设置）
engine.echo = False

# 固定大 ID，避免撞种子 / 既有测试数据
TEST_DOC_ID = 990000000000000902
CHUNK_ID = "smoke-visibility-sync-chunk-1"

# 种子团队（只作字段值，不校验存在性；kh_document.team_id 无外键）
TEAM_BACKEND = "8000000000000000002"   # 后端开发组（旧值）
TEAM_FRONTEND = "750598282475999232"   # 前端开发组（新值）

# 操作者 = 文档作者本人（写权校验只放行 ADMIN 或作者）
AUTHOR = AuthUser(user_id="1000000000000000003", username="smoke-vis-sync", roles=["ROLE_USER"])

document_service = DocumentService()

OLD_CONTENT = "旧正文：这一版在发布时建了索引，需审模式下不应被重建。"
NEW_CONTENT = "新正文：本次与可见性同时修改，可见性必须立刻同步到三处索引。"


async def cleanup() -> None:
    """物理清理：ES 两索引 → Neo4j → PG → Mongo（幂等，可先跑）"""
    c = es_service.client
    if c is not None:
        for index, doc_id in ((DOCUMENT_INDEX, str(TEST_DOC_ID)), (CHUNK_INDEX, CHUNK_ID)):
            try:
                await c.delete(index=index, id=doc_id, refresh=True)
            except Exception:
                pass
    driver = graph_build_service._driver
    if driver is not None:
        async with driver.session() as gsession:
            # 必须 consume（_run_write 内部调用），否则删除可能不被服务端观察到
            await graph_build_service._run_write(
                gsession,
                "MATCH (d:KnowledgeDocument {id: $id}) DETACH DELETE d",
                id=str(TEST_DOC_ID),
            )
    async with async_session_factory() as session:
        result = await session.execute(
            sa_delete(DocumentEntity).where(DocumentEntity.id == TEST_DOC_ID)
        )
        await session.commit()  # 不 commit 会被回滚，留下残留行
        deleted_pg = int(result.rowcount or 0)  # type: ignore[attr-defined]
    removed = await get_document_content_collection().delete_one(
        {"documentId": str(TEST_DOC_ID)}
    )
    print(
        f"   清理：PG 删 {deleted_pg} 行，Mongo 删 {removed.deleted_count} 条，ES/Neo4j 记录已删"
    )


async def prepare() -> None:
    """造「已发布 + 公开 + 后端开发组」的文档，并在三处索引写入旧值快照"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    content_id = str(ObjectId())
    await get_document_content_collection().insert_one(
        {
            "_id": ObjectId(content_id),
            "documentId": str(TEST_DOC_ID),
            "content": OLD_CONTENT,
            "contentLength": len(OLD_CONTENT),
            "contentSummary": OLD_CONTENT[:50],
            "version": 1,
            "deleted": False,
        }
    )
    async with async_session_factory() as session:
        session.add(
            DocumentEntity(
                id=TEST_DOC_ID,
                title="可见性同步冒烟文档",
                content_id=content_id,
                summary=OLD_CONTENT[:50],
                status=int(DocumentStatus.PUBLISHED),
                is_public=True,
                team_id=int(TEAM_BACKEND),
                author_id=int(AUTHOR.user_id),
                create_by=int(AUTHOR.user_id),
                update_by=int(AUTHOR.user_id),
                deleted=False,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    # ES kh_document：走项目自身的写入路径（与发布时一致）
    await search_index_service.index_document(
        {
            "id": str(TEST_DOC_ID),
            "title": "可见性同步冒烟文档",
            "summary": OLD_CONTENT[:50],
            "content": OLD_CONTENT,
            "status": int(DocumentStatus.PUBLISHED),
            "isPublic": True,
            "teamId": TEAM_BACKEND,
            "authorId": AUTHOR.user_id,
        }
    )

    # ES kh_chunk：字段名 snake_case（与 chunk 侧冗余列一致）
    c = es_service.client
    assert c is not None, "ES 客户端不可用"
    await c.index(
        index=CHUNK_INDEX,
        id=CHUNK_ID,
        document={
            "document_id": str(TEST_DOC_ID),
            "chunk_id": CHUNK_ID,
            "chunk_index": 0,
            "content": OLD_CONTENT,
            "document_title": "可见性同步冒烟文档",
            "doc_status": int(DocumentStatus.PUBLISHED),
            "is_public": True,
            "team_id": TEAM_BACKEND,
            "author_id": AUTHOR.user_id,
            "total_chunks": 1,
        },
        refresh=True,
    )

    # Neo4j：文档节点属性 camelCase（与 graph_build 写入一致）
    driver = graph_build_service._driver
    assert driver is not None, "Neo4j 驱动不可用"
    async with driver.session() as gsession:
        await graph_build_service._run_write(
            gsession,
            """
            MERGE (d:KnowledgeDocument {id: $id})
            SET d.title = $title, d.isPublic = true,
                d.teamId = $teamId, d.authorId = $authorId
            """,
            id=str(TEST_DOC_ID),
            title="可见性同步冒烟文档",
            teamId=TEAM_BACKEND,
            authorId=AUTHOR.user_id,
        )


async def main() -> None:
    print("=== 已发布文档「正文 + 可见性」同改 → 三处索引同步冒烟 ===")

    # 0) 环境自检（ES 端口通 ≠ 就绪，start() 会 verify）
    await es_service.start()
    assert es_service.client is not None, "ES 客户端不可用，请检查 docker/elasticsearch"
    await graph_build_service.start()
    assert graph_build_service._driver is not None, (
        "Neo4j 驱动不可用，请检查 docker/neo4j"
    )

    await cleanup()  # 幂等清残留
    await prepare()
    print("   0) 前置 ✓ 已发布 + 公开 + 后端开发组，三处索引写入旧值快照")

    # 1) 需审模式下 PATCH：正文 + 可见性同时改
    original_flag = settings.DOCUMENT_REQUIRE_APPROVAL
    settings.DOCUMENT_REQUIRE_APPROVAL = "true"
    try:
        async with async_session_factory() as session:
            await document_service.update(
                session,
                str(TEST_DOC_ID),
                DocumentUpdate(
                    content=NEW_CONTENT, is_public=False, team_id=TEAM_FRONTEND
                ),
                AUTHOR,
            )
    finally:
        settings.DOCUMENT_REQUIRE_APPROVAL = original_flag
    print("   1) PATCH ✓ 正文 + is_public=false + team_id=前端开发组（需审模式）")

    # 2) Mongo 版本递增 → 本次确实是 content_changed=True
    mdoc = await get_document_content_collection().find_one(
        {"documentId": str(TEST_DOC_ID)}
    )
    assert mdoc is not None
    assert mdoc["content"] == NEW_CONTENT, mdoc["content"][:20]
    assert mdoc["version"] == 2, (
        f"version 应为 1→2，实际 {mdoc['version']}；"
        "若不是，说明本次 PATCH 没带正文、场景前提不成立"
    )
    print("   2) Mongo ✓ version 1 → 2（正文确实变了，场景成立）")

    # 3) PG 控制项
    async with async_session_factory() as session:
        entity = await session.get(DocumentEntity, TEST_DOC_ID)
        assert entity is not None
        assert entity.is_public is False, entity.is_public
        assert str(entity.team_id) == TEAM_FRONTEND, entity.team_id
        assert int(entity.status) == int(DocumentStatus.PUBLISHED), entity.status
    print("   3) PG ✓ is_public=false / team_id=前端开发组 / status 仍为已发布")

    # 4) ES kh_document：可见性字段必须已刷成新值
    c = es_service.client
    assert c is not None
    src = (await c.get(index=DOCUMENT_INDEX, id=str(TEST_DOC_ID)))["_source"]
    assert src["isPublic"] is False, (
        f"ES kh_document.isPublic 仍是 {src['isPublic']} —— 需审模式下可见性同步被丢弃，"
        "检索会继续按「公开」放行"
    )
    assert src["teamId"] == TEAM_FRONTEND, (
        f"ES kh_document.teamId 仍是 {src['teamId']}（旧团队），团队可见性判定失效"
    )
    print("   4) ES kh_document ✓ isPublic=false / teamId=前端开发组")

    # 5) ES kh_chunk：同上（chunk 混合检索的关键词通道）
    csrc = (await c.get(index=CHUNK_INDEX, id=CHUNK_ID))["_source"]
    assert csrc["is_public"] is False, (
        f"ES kh_chunk.is_public 仍是 {csrc['is_public']} —— chunk 级检索仍能命中该文档"
    )
    assert csrc["team_id"] == TEAM_FRONTEND, (
        f"ES kh_chunk.team_id 仍是 {csrc['team_id']}"
    )
    print("   5) ES kh_chunk ✓ is_public=false / team_id=前端开发组")

    # 6) Neo4j 文档节点：图谱四端点按这三个属性过滤
    driver = graph_build_service._driver
    assert driver is not None
    async with driver.session() as gsession:
        res = await gsession.run(
            "MATCH (d:KnowledgeDocument {id: $id}) "
            "RETURN d.isPublic AS p, d.teamId AS t",
            id=str(TEST_DOC_ID),
        )
        record = await res.single()
    assert record is not None, "Neo4j 文档节点丢失"
    assert record["p"] is False, (
        f"Neo4j d.isPublic 仍是 {record['p']} —— /graph/* 仍会返回该文档及其实体"
    )
    assert record["t"] == TEAM_FRONTEND, f"Neo4j d.teamId 仍是 {record['t']}"
    print("   6) Neo4j ✓ isPublic=false / teamId=前端开发组")

    # 7) 需审语义：只就地刷可见性字段，不重建索引（正文保持旧值）
    assert src["content"] == OLD_CONTENT, (
        "需审模式下不应重建索引（正文应仍是旧值，等审核通过后重灌）；"
        f"实际前 20 字：{str(src.get('content'))[:20]}"
    )
    assert src["title"] == "可见性同步冒烟文档", src.get("title")
    print("   7) 需审语义 ✓ 未重建索引（ES 正文仍为旧值），仅就地刷可见性字段")

    await cleanup()
    print("\n可见性同步冒烟测试全部通过 ✅")


async def run() -> None:
    """main + cleanup 同一事件循环（engine 连接池绑定首个 loop，不能二次 run）"""
    try:
        await main()
    except Exception:
        print("冒烟测试失败，执行清理", file=sys.stderr)
        await cleanup()
        raise
    finally:
        await graph_build_service.close()
        await es_service.close()
        await close_mongo_client()


if __name__ == "__main__":
    asyncio.run(run())
