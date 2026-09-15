"""文档列表 / 详情可见性 E2E 冒烟（真实 PG + Mongo；裸 FastAPI app + ASGITransport）

对应 TS `DocumentService.findAll(query, user)` 与 `findOne(id, true, user)`：
普通用户只能看到「自己写的 ∪ 已发布且公开 ∪ 已发布且所在团队」，
ADMIN / REVIEWER 不受限。

覆盖：
  1) GET /documents       HTTP 视角：user / admin / reviewer 三份可见集合
  2) GET /documents/{id}  HTTP 视角：可读 200，不可读 403
  3) 服务层 find_all / find_one 与 HTTP 同口径（scope=None = 不过滤、
     unrestricted = 全量），防止「端点接线对但服务层漏判」
  4) 可见性变更（公开转私有 + 换到非成员团队）后列表 / 详情立即失效 ——
     本链路是 PG 实时判定，不依赖索引，改权限必须马上生效
  5) 零残留

数据准备（标题共用唯一前缀，便于精确断言，不受库里既有语料干扰）：
  PUB         is_public=true,  status=1, author=admin
  OWN         is_public=false, status=1, author=user(003)   → 仅本人可见
  OWN_DRAFT   is_public=false, status=0, author=user(003)   → 自己的草稿也要可见
  TEAM_IN     is_public=false, status=1, author=admin, team=user 所在团队
  TEAM_OUT    is_public=false, status=1, author=admin, team=user 不在的团队
  OTHER_DRAFT is_public=false, status=0, author=admin       → 他人的草稿不可见

注意：document.create 恒以登录者为作者（对齐 TS，防伪造 authorId），
故「本人」用例必须用该用户自己的 token 创建，不能由 admin 代建。

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python \
        scripts/smoke_document_visibility.py
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")

import httpx  # noqa: E402
from sqlalchemy import delete, text  # noqa: E402

from app.common.document_access import access_from_user  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.mongo import get_document_content_collection  # noqa: E402
from app.db.session import async_session_factory, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.document import DocumentEntity  # noqa: E402
from app.schemas.auth import AuthUser  # noqa: E402
from app.schemas.document import DocumentQuery  # noqa: E402
from app.services.document import DocumentService  # noqa: E402
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

# 免审：status=1 的创建请求才不会被 400 拒绝
settings.DOCUMENT_REQUIRE_APPROVAL = "false"

_results: list[str] = []


def check(step: str, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    _results.append(f"{mark} {step}: {detail}")
    if not ok:
        raise AssertionError(f"{step} 失败：{detail}")


async def _create_doc(
    client: httpx.AsyncClient,
    title: str,
    is_public: bool,
    status: int,
    team_id: str | None = None,
    headers: dict | None = None,
) -> str:
    """创建一篇文档（不触发索引；本链路只验 PG 元数据可见性）"""
    hdrs = headers or bearer_headers()
    payload: dict = {"title": title, "content": f"{title} 正文内容。", "status": status,
                     "isPublic": is_public}
    if team_id:
        payload["teamId"] = team_id
    r = await client.post(f"{BASE}/documents", json=payload, headers=hdrs)
    assert r.status_code == 201, f"创建文档失败：{r.status_code} {r.text[:200]}"
    return str(r.json()["id"])


async def _list_ids(
    client: httpx.AsyncClient, headers: dict, prefix: str
) -> tuple[set[str], int]:
    """GET /documents?title=<prefix>，返回（命中 id 集合, total）"""
    r = await client.get(
        f"{BASE}/documents",
        params={"title": prefix, "pageSize": 50},
        headers=headers,
    )
    assert r.status_code == 200, f"列表查询失败：{r.status_code} {r.text[:200]}"
    body = r.json()
    return {str(it["id"]) for it in body["items"]}, int(body["total"])


async def _get_doc(client: httpx.AsyncClient, headers: dict, doc_id: str) -> int:
    """GET /documents/{id}，返回状态码"""
    r = await client.get(f"{BASE}/documents/{doc_id}", headers=headers)
    return r.status_code


async def _cleanup_docs(doc_ids: list[str]) -> None:
    """物理删除文档（PG 主表 + Mongo 正文）"""
    async with engine.begin() as conn:
        await conn.execute(
            delete(DocumentEntity).where(
                DocumentEntity.id.in_([int(d) for d in doc_ids])
            )
        )
    col = get_document_content_collection()
    for doc_id in doc_ids:
        await col.delete_one({"documentId": str(doc_id)})


async def _residue(doc_ids: list[str]) -> dict[str, int]:
    """残留检查：PG 文档 / doc_chunk / Mongo 正文三处计数"""
    async with async_session_factory() as session:
        doc_count = await session.scalar(
            text("SELECT count(*) FROM kh_document WHERE id = ANY(:ids)"),
            {"ids": [int(d) for d in doc_ids]},
        )
        chunk_count = await session.scalar(
            text("SELECT count(*) FROM doc_chunk WHERE document_id = ANY(:ids)"),
            {"ids": [int(d) for d in doc_ids]},
        )
    mongo_count = await get_document_content_collection().count_documents(
        {"documentId": {"$in": [str(d) for d in doc_ids]}}
    )
    return {
        "pg_doc": int(doc_count or 0),
        "pg_chunk": int(chunk_count or 0),
        "mongo": int(mongo_count),
    }


async def main() -> None:
    print("=== 文档列表 / 详情可见性 E2E 冒烟 ===")
    engine.echo = False

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
    print(f"   user(003) 团队={my_team_ids}，成员团队={member_team}，非成员团队={other_team}")

    doc_ids: list[str] = []
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            tag = str(int(time.time()))[-6:]
            prefix = f"文档可见性冒烟{tag}"
            user_headers = bearer_headers(SEED_USER_ID, SEED_USER_USERNAME)
            admin_headers = bearer_headers(SEED_ADMIN_ID, SEED_ADMIN_USERNAME)
            reviewer_headers = bearer_headers(
                SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME
            )

            # 1) 准备 6 篇可见性各异的文档
            pub = await _create_doc(
                client, f"{prefix}-PUB", True, 1, headers=admin_headers
            )
            own = await _create_doc(
                client, f"{prefix}-OWN", False, 1, headers=user_headers
            )
            own_draft = await _create_doc(
                client, f"{prefix}-OWNDRAFT", False, 0, headers=user_headers
            )
            team_in = await _create_doc(
                client, f"{prefix}-TEAMIN", False, 1, team_id=member_team,
                headers=admin_headers,
            )
            team_out = await _create_doc(
                client, f"{prefix}-TEAMOUT", False, 1, team_id=other_team,
                headers=admin_headers,
            )
            other_draft = await _create_doc(
                client, f"{prefix}-OTHERDRAFT", False, 0, headers=admin_headers
            )
            doc_ids = [pub, own, own_draft, team_in, team_out, other_draft]
            print(f"   准备：6 篇文档 pub={pub} own={own} ownDraft={own_draft} "
                  f"in={team_in} out={team_out} otherDraft={other_draft}")

            # 2) 列表可见性矩阵（HTTP）
            user_expected = {pub, own, own_draft, team_in}
            user_ids, user_total = await _list_ids(client, user_headers, prefix)
            check(
                "列表可见集合（user）",
                user_ids == user_expected,
                f"期望 {sorted(user_expected)}，实际 {sorted(user_ids)}",
            )
            check(
                "列表 total 与可见集合一致（可见性条件同时作用于 count 与分页）",
                user_total == len(user_expected),
                f"total={user_total}，应等于 {len(user_expected)}",
            )
            check(
                "他人的未发布文档不可见（TEAM_OUT / OTHER_DRAFT）",
                team_out not in user_ids and other_draft not in user_ids,
                f"实际集合 {sorted(user_ids)}",
            )
            check(
                "自己的草稿可见（作者分支不限 status）",
                own_draft in user_ids,
                f"own_draft={own_draft} 应在列表中；实际 {sorted(user_ids)}",
            )

            admin_ids, admin_total = await _list_ids(client, admin_headers, prefix)
            check(
                "列表可见集合（admin 不受限）",
                admin_ids == set(doc_ids),
                f"期望全部 {len(doc_ids)} 篇，实际 {sorted(admin_ids)}",
            )
            check(
                "admin total 为全量",
                admin_total == len(doc_ids),
                f"total={admin_total}，应等于 {len(doc_ids)}",
            )

            reviewer_ids, _ = await _list_ids(client, reviewer_headers, prefix)
            check(
                "列表可见集合（reviewer 不受限）",
                reviewer_ids == set(doc_ids),
                f"期望全部 {len(doc_ids)} 篇，实际 {sorted(reviewer_ids)}",
            )

            # 3) 详情可见性矩阵（HTTP）
            cases_readable = [
                ("公开文档", pub, user_headers),
                ("本人已发布", own, user_headers),
                ("本人草稿", own_draft, user_headers),
                ("成员团队文档", team_in, user_headers),
            ]
            for label, doc_id, hdrs in cases_readable:
                status_code = await _get_doc(client, hdrs, doc_id)
                check(
                    f"详情可读（{label}）",
                    status_code == 200,
                    f"期望 200，实际 {status_code}",
                )

            status_code = await _get_doc(client, user_headers, team_out)
            check(
                "详情不可读（非成员团队已发布）→ 403",
                status_code == 403,
                f"期望 403，实际 {status_code}",
            )
            status_code = await _get_doc(client, user_headers, other_draft)
            check(
                "详情不可读（他人草稿）→ 403",
                status_code == 403,
                f"期望 403，实际 {status_code}",
            )
            for label, doc_id, hdrs in [
                ("admin", team_out, admin_headers),
                ("reviewer", other_draft, reviewer_headers),
            ]:
                status_code = await _get_doc(client, hdrs, doc_id)
                check(
                    f"详情不受限（{label}）",
                    status_code == 200,
                    f"期望 200，实际 {status_code}",
                )

            # 4) 服务层同口径（防「端点接线对但服务层漏判」）
            svc = DocumentService()
            user_auth = AuthUser(
                userId=SEED_USER_ID,
                username=SEED_USER_USERNAME,
                roles=["ROLE_USER"],
                teamIds=my_team_ids,
            )
            admin_auth = AuthUser(
                userId=SEED_ADMIN_ID,
                username=SEED_ADMIN_USERNAME,
                roles=["ROLE_ADMIN"],
            )
            query = DocumentQuery(title=prefix, page=1, page_size=50)
            async with async_session_factory() as session:
                scoped = await svc.find_all(session, query, access_from_user(user_auth))
                unscoped = await svc.find_all(session, query)  # scope=None → 不过滤
                admin_scoped = await svc.find_all(
                    session, query, access_from_user(admin_auth)
                )
            check(
                "服务层 find_all（user scope）",
                {str(i["id"]) for i in scoped["items"]} == user_expected,
                f"期望 {sorted(user_expected)}，实际 "
                f"{sorted(str(i['id']) for i in scoped['items'])}",
            )
            check(
                "服务层 find_all（scope=None 不过滤）",
                {str(i["id"]) for i in unscoped["items"]} == set(doc_ids),
                "None 语义是不过滤（与 search_documents / graph 查询侧一致），"
                f"实际 {sorted(str(i['id']) for i in unscoped['items'])}",
            )
            check(
                "服务层 find_all（admin unrestricted）",
                {str(i["id"]) for i in admin_scoped["items"]} == set(doc_ids),
                f"期望全部 {len(doc_ids)} 篇，"
                f"实际 {sorted(str(i['id']) for i in admin_scoped['items'])}",
            )

            async with async_session_factory() as session:
                detail_ok = await svc.find_one(
                    session, pub, scope=access_from_user(user_auth)
                )
                detail_unscoped = await svc.find_one(session, team_out)
                try:
                    await svc.find_one(
                        session, team_out, scope=access_from_user(user_auth)
                    )
                    denied = False
                except Exception as err:  # noqa: BLE001 —— 期望 403
                    denied = getattr(err, "status_code", None) == 403
            check(
                "服务层 find_one（可读）",
                detail_ok["id"] == pub,
                f"应返回 {pub}，实际 {detail_ok.get('id')}",
            )
            check(
                "服务层 find_one（scope=None 不判权）",
                detail_unscoped["id"] == team_out,
                "None 语义是不判权（对齐 TS `user?: AuthUser`），"
                f"实际 {detail_unscoped.get('id')}",
            )
            check("服务层 find_one（不可读 → 403）", denied, "应抛 403 无权查看该文档")

            # 5) 可见性变更后立即失效（PG 实时判定，无索引延迟）
            r = await client.patch(
                f"{BASE}/documents/{pub}",
                json={"isPublic": False, "teamId": other_team},
                headers=admin_headers,
            )
            assert r.status_code == 200, f"改可见性失败：{r.status_code} {r.text[:200]}"
            after_ids, after_total = await _list_ids(client, user_headers, prefix)
            check(
                "改私有 + 换非成员团队后立即不可见（列表）",
                pub not in after_ids and after_total == len(user_expected) - 1,
                f"实际集合 {sorted(after_ids)}，total={after_total}",
            )
            status_code = await _get_doc(client, user_headers, pub)
            check(
                "改私有 + 换非成员团队后立即不可见（详情 403）",
                status_code == 403,
                f"期望 403，实际 {status_code}",
            )
            admin_after, _ = await _list_ids(client, admin_headers, prefix)
            check(
                "可见性变更不影响 admin",
                admin_after == set(doc_ids),
                f"admin 应仍看到全部 {len(doc_ids)} 篇，实际 {sorted(admin_after)}",
            )
    finally:
        if doc_ids:
            await _cleanup_docs(doc_ids)

    # 6) 零残留（断言必须在清理之后）
    if doc_ids:
        res = await _residue(doc_ids)
        check(
            "零残留",
            res["pg_doc"] == 0 and res["pg_chunk"] == 0 and res["mongo"] == 0,
            f"残留 {res}",
        )
        print(f"   清理：6 篇文档已移除，残留检查 {res}")

    print()
    for line in _results:
        print(line)
    print(f"\n文档列表 / 详情可见性冒烟全部通过 ✅（{len(_results)} 项断言）")


if __name__ == "__main__":
    asyncio.run(main())
