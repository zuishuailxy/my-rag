"""文档检索可见性 E2E 冒烟（真实 PG + Mongo + ES；裸 FastAPI app + ASGITransport）

覆盖范围：公开 ∪ 本人 ∪ 所在团队 三条可见路径，以及 ADMIN/REVIEWER 不受限。

数据准备：创建 4 篇已发布文档（同一关键词，便于一次查询全部命中）：
  PUB          is_public=true,  author=admin
  OWN          is_public=false, author=user(种子 003)   → 仅本人可见
  TEAM_MEMBER  is_public=false, author=admin, team=user 所在团队
  TEAM_OTHER   is_public=false, author=admin, team=user 不在的团队

断言两条检索链路，各自覆盖上述可见性矩阵：
  1) chunk 级 POST /api/v1/rag/search（ES kh_chunk 关键词 + PG doc_chunk 向量 → RRF → rerank）
     - HTTP 接线（含 teamIds 注入）
     - HybridRetrievalService(min_score=0) 直接调用：命中集合恰为 {PUB, OWN, TEAM_MEMBER}
     - ES 关键词通道 filter 单测（该通道环境性退化时跳过）
  2) 文档级 POST /api/v1/search（ES kh_document 全文检索）
     - HTTP 可见性矩阵（user / admin / reviewer 三视角）
     - kh_document 的 teamId 字段写入核验（缺失会让团队可见性静默失效）
     - 可见性变更（公开转私有、正文不变）后立即失效的安全回归

文档级索引的写入路径：真实链路是「发布 → MQ INDEX → orchestrator 用
pipeline.types.to_search_index_doc 转换 → SearchIndexService.index_document」；
冒烟禁用了 MQ（RABBITMQ_ENABLED=false），故在进程内调用同一转换与写入函数补上，
不另造一套字段映射。

清理：4 篇文档物理删除（ES kh_chunk + ES kh_document + PG 级联 + Mongo），并断言零残留。
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")

import httpx  # noqa: E402
from sqlalchemy import delete, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.es import es_service  # noqa: E402
from app.db.mongo import get_document_content_collection  # noqa: E402
from app.db.session import async_session_factory, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.document import DocumentEntity  # noqa: E402
from app.pipeline.types import to_search_index_doc  # noqa: E402
from app.schemas.auth import AuthUser  # noqa: E402
from app.services.es_index import CHUNK_INDEX  # noqa: E402
from app.services.indexing import load_pipeline_document  # noqa: E402
from app.services.retrieval import HybridRetrievalService  # noqa: E402
from app.services.search import _keyword_recall  # noqa: E402
from app.services.search_index import DOCUMENT_INDEX, search_index_service  # noqa: E402
from app.services.team import team_service  # noqa: E402

sys.path.insert(0, "scripts")
from smoke_auth import (  # noqa: E402
    SEED_ADMIN_ID,
    SEED_ADMIN_USERNAME,
    SEED_REVIEWER_ID,
    SEED_REVIEWER_USERNAME,
    SEED_USER_ID,
    SEED_USER_USERNAME,
    bearer_headers,
)

BASE = "http://testserver/api/v1"

# 免审（创建即发布，走真实索引链路）
settings.DOCUMENT_REQUIRE_APPROVAL = "false"

# 唯一关键词：4 篇文档共用，一次查询即可覆盖全部候选
KEYWORD = "可见性冒烟锚点令牌"
# 正文够长以保证切成多块（每篇至少 1 块进 PG + ES）
BODY = "\n\n".join(
    [f"## 第 {i} 节\n\n{KEYWORD}。" + ("知识库文档可见性由公开标志、作者与所属团队共同决定。" * 40) for i in range(3)]
)

_results: list[str] = []
_skipped: list[str] = []


def check(step: str, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    _results.append(f"{mark} {step}: {detail}")
    if not ok:
        raise AssertionError(f"{step} 失败：{detail}")


def skip(step: str, detail: str) -> None:
    _skipped.append(f"⚠️  {step}: {detail}")


async def _create_doc(
    client: httpx.AsyncClient, title: str, is_public: bool, team_id: str | None,
    headers: dict | None = None,
) -> str:
    """创建并索引一篇已发布文档，返回文档 ID

    headers 决定作者是谁：document.create 恒以登录者为作者（对齐 TS，防伪造 authorId），
    故「本人私有」用例必须用该用户自己的 token 创建，不能由 admin 代建。
    """
    hdrs = headers or bearer_headers()
    payload: dict = {
        "title": title,
        "content": BODY,
        "status": 1,
        "isPublic": is_public,
    }
    if team_id:
        payload["teamId"] = team_id
    r = await client.post(f"{BASE}/documents", json=payload, headers=hdrs)
    assert r.status_code == 201, f"创建文档失败：{r.status_code} {r.text[:200]}"
    doc_id = r.json()["id"]
    r = await client.post(f"{BASE}/documents/{doc_id}/index", headers=hdrs)
    assert r.status_code == 200, f"索引失败：{r.status_code} {r.text[:200]}"
    return doc_id


async def _cleanup_docs(doc_ids: list[str]) -> None:
    """物理删除文档（ES kh_chunk + ES kh_document + PG 级联 doc_chunk + Mongo 正文）"""
    c = es_service.client
    if c is not None:
        for doc_id in doc_ids:
            try:
                await c.delete_by_query(
                    index=CHUNK_INDEX,
                    query={"term": {"document_id": doc_id}},
                    refresh=True,
                )
            except Exception:  # noqa: BLE001 —— 索引不存在视为已清空
                pass
        try:
            # 文档级索引 _id 即文档 ID（index_document 以 doc_id 作 _id）
            await c.delete_by_query(
                index=DOCUMENT_INDEX,
                query={"ids": {"values": list(doc_ids)}},
                refresh=True,
            )
        except Exception:  # noqa: BLE001
            pass
    async with engine.begin() as conn:
        await conn.execute(
            delete(DocumentEntity).where(
                DocumentEntity.id.in_([int(d) for d in doc_ids])
            )
        )
    col = get_document_content_collection()
    for doc_id in doc_ids:
        await col.delete_one({"documentId": str(doc_id)})


async def _residue(doc_ids: list[str]) -> dict:
    """残留检查：PG 文档 / doc_chunk / ES kh_chunk 三处计数"""
    async with async_session_factory() as session:
        doc_count = (
            await session.execute(
                text("SELECT count(*) FROM kh_document WHERE id = ANY(:ids)"),
                {"ids": [int(d) for d in doc_ids]},
            )
        ).scalar()
        chunk_count = (
            await session.execute(
                text("SELECT count(*) FROM doc_chunk WHERE document_id = ANY(:ids)"),
                {"ids": [int(d) for d in doc_ids]},
            )
        ).scalar()
    es_count: int | str = "ES 不可用"
    es_doc_count: int | str = "ES 不可用"
    c = es_service.client
    if c is not None:
        resp = await c.count(
            index=CHUNK_INDEX, query={"terms": {"document_id": doc_ids}}
        )
        es_count = resp.get("count", 0)
        resp = await c.count(index=DOCUMENT_INDEX, query={"ids": {"values": doc_ids}})
        es_doc_count = resp.get("count", 0)
    return {
        "pg_doc": doc_count,
        "pg_chunk": chunk_count,
        "es_chunk": es_count,
        "es_doc": es_doc_count,
    }


async def _hit_doc_ids(client: httpx.AsyncClient, headers: dict) -> set[str]:
    """调用 /rag/search（topK=20），返回命中的 documentId 集合"""
    r = await client.post(
        f"{BASE}/rag/search",
        json={"query": KEYWORD, "topK": 20},
        headers=headers,
    )
    assert r.status_code == 200, f"rag/search 失败：{r.status_code} {r.text[:200]}"
    return {it["documentId"] for it in r.json()}


async def _index_documents_es(doc_ids: list[str]) -> int:
    """把本次 4 篇文档写入 ES kh_document（文档级全文索引）

    真实链路是「发布 → MQ INDEX → orchestrator 用 pipeline.types.to_search_index_doc
    转换 → SearchIndexService.index_document」。冒烟禁用了 MQ，这里在进程内走同一份
    转换函数与写入方法，保证验证的是生产逻辑而非测试自造逻辑。返回写入条数。
    """
    written = 0
    async with async_session_factory() as session:
        for doc_id in doc_ids:
            doc = await load_pipeline_document(session, int(doc_id))
            if doc is None:
                continue
            await search_index_service.index_document(to_search_index_doc(doc))
            written += 1
    return written


async def _hit_document_ids(client: httpx.AsyncClient, headers: dict) -> set[str]:
    """调用文档级 /search（pageSize=50），返回命中的文档 ID 集合"""
    r = await client.post(
        f"{BASE}/search",
        json={"keyword": KEYWORD, "pageSize": 50},
        headers=headers,
    )
    assert r.status_code == 200, f"/search 失败：{r.status_code} {r.text[:200]}"
    return {it["id"] for it in r.json()["items"]}


async def main() -> None:
    print("=== 文档检索可见性 E2E 冒烟 ===")
    engine.echo = False  # 压掉 SQLAlchemy echo

    await es_service.start()
    assert es_service.client is not None, "ES 客户端不可用，请检查 docker/elasticsearch"

    # 0) 团队关系：取一个 user(003) 所在团队 + 一个不在的团队（避免硬编码种子 ID）
    async with async_session_factory() as session:
        my_team_ids = await team_service.list_user_team_ids(session, SEED_USER_ID)
        teams = (await session.execute(text("SELECT id FROM kh_team"))).scalars().all()
    all_team_ids = [str(t) for t in teams]
    assert my_team_ids, f"种子用户 {SEED_USER_ID} 无团队，冒烟前提不成立"
    member_team = my_team_ids[0]
    others = [t for t in all_team_ids if t not in my_team_ids]
    assert others, "库里只有一个团队，无法构造「非成员团队」用例"
    other_team = others[0]
    print(f"   user(003) 团队={my_team_ids}，测试用成员团队={member_team}，非成员团队={other_team}")

    doc_ids: list[str] = []
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            tag = str(int(time.time()))[-6:]
            user_headers = bearer_headers(SEED_USER_ID, SEED_USER_USERNAME)
            admin_headers = bearer_headers(SEED_ADMIN_ID, SEED_ADMIN_USERNAME)
            reviewer_headers = bearer_headers(SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME)

            # 1) 准备 4 篇可见性各异的已发布文档（同一关键词）
            pub = await _create_doc(client, f"可见性冒烟PUB{tag}", True, None)
            # 本人私有：必须用 user 自己的 token 创建（create 恒以登录者为作者）
            own = await _create_doc(
                client, f"可见性冒烟OWN{tag}", False, None, headers=user_headers
            )
            team_in = await _create_doc(
                client, f"可见性冒烟TEAMIN{tag}", False, member_team
            )
            team_out = await _create_doc(
                client, f"可见性冒烟TEAMOUT{tag}", False, other_team
            )
            doc_ids = [pub, own, team_in, team_out]
            print(f"   准备：4 篇文档已发布并索引 pub={pub} own={own} in={team_in} out={team_out}")

            # 1.5) 文档级索引（ES kh_document）：按生产同一路径写入，供 /search 可见性验证
            written = await _index_documents_es(doc_ids)
            check(
                "kh_document 写入",
                written == len(doc_ids),
                f"应写入 {len(doc_ids)} 篇，实际 {written} 篇",
            )
            # teamId 必须落盘且值正确：缺失会让「团队可见」这条路径静默失效
            # （若靠 ES 动态映射，字符串字段会建成 text，terms 查询按分词词项匹配 → 匹配不到）
            # 只看两篇确有团队的文档（PUB / OWN 未指定团队，teamId=None 时 ES 不落该字段）
            src_hits = (
                await es_service.client.search(
                    index=DOCUMENT_INDEX,
                    query={"ids": {"values": [team_in, team_out]}},
                    size=10,
                    source={"includes": ["id", "teamId", "isPublic", "authorId"]},
                )
            )["hits"]["hits"]
            src_by_id = {h["_source"].get("id"): h["_source"] for h in src_hits}
            check(
                "kh_document teamId 写入",
                len(src_by_id) == 2
                and src_by_id.get(team_in, {}).get("teamId") == member_team
                and src_by_id.get(team_out, {}).get("teamId") == other_team,
                f"期望 teamId 分别为 {member_team} / {other_team}，实际 {src_by_id}",
            )

            # 2) ES kh_chunk 落盘（确认索引写入生效，且 is_public 字段写入成功）
            raw = await es_service.client.search(
                index=CHUNK_INDEX,
                query={"terms": {"document_id": doc_ids}},
                size=0,
                aggs={"by_public": {"terms": {"field": "is_public"}}},
            )
            es_total = raw["hits"]["total"]["value"]
            if es_total == 0:
                skip(
                    "ES kh_chunk 落盘",
                    "ES 关键词通道环境性退化（写入 0 条），关键词侧过滤无法端到端验证",
                )
            else:
                buckets = {
                    str(b["key"]): b["doc_count"]
                    for b in raw["aggregations"]["by_public"]["buckets"]
                }
                # 缺 is_public 字段的文档数（布尔字段不支持 terms.missing，单独用 must_not exists 查）
                missing = await es_service.client.count(
                    index=CHUNK_INDEX,
                    query={
                        "bool": {
                            "must": [{"terms": {"document_id": doc_ids}}],
                            "must_not": [{"exists": {"field": "is_public"}}],
                        }
                    },
                )
                check(
                    "ES kh_chunk is_public 写入",
                    missing.get("count", 0) == 0,
                    f"应有 {es_total} 条 chunk 全部带 is_public，实际缺字段 {missing.get('count')} 条；"
                    f"is_public 分布={buckets}",
                )

            # 3) 普通用户：公开 ∪ 本人 ∪ 所在团队；他人团队不可见
            user_hits = await _hit_doc_ids(client, user_headers)
            visible_for_user = {pub, own, team_in}
            check(
                "普通用户可见范围（HTTP）",
                visible_for_user <= user_hits,
                f"应命中 {len(visible_for_user)} 篇，实际命中 {sorted(user_hits)}",
            )
            check(
                "普通用户不可见他人团队文档（HTTP）",
                team_out not in user_hits,
                f"documentId={team_out}（团队 {other_team}）不应被用户看到，实际命中 {sorted(user_hits)}",
            )

            # 3.5) 文档级全文检索 POST /search：同口径可见性（user 视角）
            doc_user_hits = await _hit_document_ids(client, user_headers)
            check(
                "普通用户可见范围（/search 文档级）",
                visible_for_user <= doc_user_hits,
                f"应命中 {len(visible_for_user)} 篇，实际命中 {sorted(doc_user_hits)}",
            )
            check(
                "普通用户不可见他人团队文档（/search 文档级）",
                team_out not in doc_user_hits,
                f"documentId={team_out}（团队 {other_team}）不应被搜到，"
                f"实际命中 {sorted(doc_user_hits)}",
            )

            # 4) 管理员 / 审核员：不受限，四篇全见
            for label, headers in (("管理员", admin_headers), ("审核员", reviewer_headers)):
                hits = await _hit_doc_ids(client, headers)
                check(
                    f"{label}不受限（chunk 级 rag/search）",
                    set(doc_ids) <= hits,
                    f"{label}应看到全部 4 篇，实际命中 {sorted(hits)}",
                )
                doc_hits = await _hit_document_ids(client, headers)
                check(
                    f"{label}不受限（/search 文档级）",
                    set(doc_ids) <= doc_hits,
                    f"{label}应搜到全部 4 篇，实际命中 {sorted(doc_hits)}",
                )

            # 5) 可见性就地变更（安全回归）：公开 → 私有且正文不变
            #    若不同步 chunk 可见性，ES 关键词侧会按旧值继续召回 → 越权可见
            r = await client.patch(
                f"{BASE}/documents/{pub}", json={"isPublic": False}, headers=admin_headers
            )
            check(
                "PATCH 改私有",
                r.status_code == 200 and r.json().get("isPublic") is False,
                f"期望 200 且 isPublic=false，实际 {r.status_code} body={r.text[:200]}",
            )
            after_patch = await _hit_doc_ids(client, user_headers)
            check(
                "改私有后普通用户立即不可见（chunk 级）",
                pub not in after_patch,
                f"documentId={pub} 已转私有，用户不应再看到；实际命中 {sorted(after_patch)}",
            )
            doc_after_patch = await _hit_document_ids(client, user_headers)
            check(
                "改私有后普通用户立即不可见（/search 文档级）",
                pub not in doc_after_patch,
                f"documentId={pub} 已转私有，用户不应再搜到；"
                f"实际命中 {sorted(doc_after_patch)}（kh_document 可见性未同步会导致此项失败）",
            )
            admin_after = await _hit_doc_ids(client, admin_headers)
            check(
                "改私有后管理员仍可见（chunk 级）",
                pub in admin_after,
                f"管理员不受限，应仍看到 {pub}；实际 {sorted(admin_after)}",
            )
            doc_admin_after = await _hit_document_ids(client, admin_headers)
            check(
                "改私有后管理员仍可见（/search 文档级）",
                pub in doc_admin_after,
                f"管理员不受限，应仍搜到 {pub}；实际 {sorted(doc_admin_after)}",
            )
            # 复位为公开，避免影响后续断言（HTTP 断言里 pub 属可见集合）
            r = await client.patch(
                f"{BASE}/documents/{pub}", json={"isPublic": True}, headers=admin_headers
            )
            assert r.status_code == 200, f"复位公开失败：{r.status_code}"

            # 6) 服务层完整性（绕过 rerank 分数下限）：命中集合应恰为可见集合
            svc = HybridRetrievalService(min_score=0)
            user_auth = AuthUser(
                userId=SEED_USER_ID,
                username=SEED_USER_USERNAME,
                roles=["ROLE_USER"],
                teamIds=my_team_ids,
            )
            rows = await svc.retrieve(KEYWORD, top_k=20, user=user_auth)
            # 只看本次 4 篇文档：库里可能有其他公开文档同样命中该关键词（语料非空）
            got = {r["documentId"] for r in rows} & set(doc_ids)
            check(
                "服务层可见集合（user）",
                got == visible_for_user,
                f"期望恰为 {sorted(visible_for_user)}，实际 {sorted(got)}",
            )

            admin_auth = AuthUser(
                userId=SEED_ADMIN_ID,
                username=SEED_ADMIN_USERNAME,
                roles=["ROLE_ADMIN"],
            )
            rows = await svc.retrieve(KEYWORD, top_k=20, user=admin_auth)
            got_admin = {r["documentId"] for r in rows}
            check(
                "服务层可见集合（admin）",
                set(doc_ids) <= got_admin,
                f"管理员应看到全部 4 篇，实际 {sorted(got_admin)}",
            )

            # 6) ES 关键词通道 filter 单测（该通道退化时跳过）
            from app.common.document_access import CHUNK_VISIBILITY_FIELDS, access_from_user

            scope_user = access_from_user(user_auth)
            kw_all = await _keyword_recall(KEYWORD, 20, None)
            if not kw_all:
                skip("ES 关键词通道过滤", "关键词通道无召回（环境性退化），跳过该通道断言")
            else:
                kw_user = await _keyword_recall(KEYWORD, 20, scope_user)
                # 关键词侧返回 chunk_id，需换算回 document_id 判断
                async with async_session_factory() as session:
                    rows2 = (
                        await session.execute(
                            text(
                                "SELECT chunk_id, document_id FROM doc_chunk "
                                "WHERE chunk_id = ANY(:ids)"
                            ),
                            {"ids": [cid for cid, _ in kw_user]},
                        )
                    ).all()
                kw_doc_ids = {str(r[1]) for r in rows2}
                check(
                    "ES 关键词通道过滤",
                    team_out not in kw_doc_ids,
                    f"关键词通道不应召回 {team_out}，实际 {sorted(kw_doc_ids)}；"
                    f"字段={list(CHUNK_VISIBILITY_FIELDS)}",
                )
    finally:
        if doc_ids:
            await _cleanup_docs(doc_ids)

    # 7) 零残留（断言必须在清理之后）
    if doc_ids:
        res = await _residue(doc_ids)
        check(
            "零残留",
            res["pg_doc"] == 0
            and res["pg_chunk"] == 0
            and res["es_chunk"] in (0, "ES 不可用")
            and res["es_doc"] in (0, "ES 不可用"),
            f"残留 {res}",
        )
        print(f"   清理：4 篇文档已移除，残留检查 {res}")

    print()
    for line in _results:
        print(line)
    for line in _skipped:
        print(line)
    print(f"\n文档检索可见性冒烟{'全部通过' if not _skipped else '通过（含跳过项）'} ✅")


if __name__ == "__main__":
    asyncio.run(main())
