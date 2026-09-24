"""全文搜索接口 E2E 冒烟（真实 PG + ES，裸 FastAPI app + ASGITransport）

覆盖 SearchController 全部路径：
  1. 未登录 → 401
  2. 无 search 权限的用户（临时用户清空角色关联 → 零权限码）→ 403
  3. 管理员关键词搜索 → 200，命中 ≥ 1，且返回命中词高亮（<em> 标签包裹关键词）
  4. 分类过滤：categoryId 生效（只命中指定分类的文档）
  5. 分页：pageSize 生效（返回条数 = pageSize，total 不裁剪）

注：403 用例不用种子 REVIEWER 测——9-3 补齐 11 行权限矩阵时 REVIEWER 也绑了 search 码
（init.sql 4100000000000000011 行），种子角色已无「无 search」者；改为临时用户清空角色。
smoke_search_hybrid.py 同款方案。

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_search_api.py

前置：docker 的 postgres / elasticsearch 已就绪（连接读 backend/.env）。
"""
import asyncio
import re
import sys
import time

import httpx
from sqlalchemy import text

from app.db.es import es_service
from app.db.session import engine
from app.main import app
from app.services.search_index import DOCUMENT_INDEX, search_index_service
from scripts.smoke_auth import bearer_headers

BASE = "http://testserver/api/v1"

# 唯一关键词：英文连续串，IK 分词稳定命中，便于断言高亮
KEYWORD = "SmokeHighlightToken"
DOC_A_ID = "smoke-search-A"
DOC_B_ID = "smoke-search-B"
CAT_A = "smoke-cat-A"
CAT_B = "smoke-cat-B"

# 冒烟用团队 ID（snowflake 形态的字符串，仅作 ES 回传断言，不参与可见性过滤：
# 本脚本用 admin（unrestricted）调用，可见性子句不会生效）
SMOKE_TEAM_ID = "3000000000000000001"

DOC_A = {
    "id": DOC_A_ID,
    "title": f"{KEYWORD} 文档A标题",
    "summary": f"{KEYWORD} 摘要A内容",
    "content": f"正文包含 {KEYWORD} 用于验证关键词高亮返回。",
    "categoryId": CAT_A,
    "authorId": "1000000000000000001",
    "tags": "smoke",
    "status": 1,
    "isPublic": True,
    # 仅用于验证 vs 可见性字段回传；本脚本直写 ES，
    # DOC_A 带团队、DOC_B 不带，覆盖「有值 / 缺字段」两种情况
    "teamId": SMOKE_TEAM_ID,
    "viewCount": 0,
    "likeCount": 0,
    "commentCount": 0,
}
DOC_B = {
    "id": DOC_B_ID,
    "title": f"{KEYWORD} 文档B标题",
    "summary": f"{KEYWORD} 摘要B内容",
    "content": f"另一篇正文同样包含 {KEYWORD} 关键词。",
    "categoryId": CAT_B,
    "authorId": "1000000000000000002",
    "tags": "smoke",
    "status": 1,
    "isPublic": True,
    "viewCount": 0,
    "likeCount": 0,
    "commentCount": 0,
}

_results: list[str] = []


def check(step: str, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    _results.append(f"{mark} {step}: {detail}")
    if not ok:
        raise AssertionError(f"{step} 失败：{detail}")


async def _create_roleless_user(client: httpx.AsyncClient) -> tuple[str, str]:
    """创建临时用户并清空其角色关联 → 零权限码（403 用例用，用完即物理删）"""
    username = f"smokesearch0{int(time.time() * 1000)}"
    r = await client.post(
        f"{BASE}/users",
        json={"username": username, "password": "123456", "roleCodes": ["ROLE_USER"]},
        headers=bearer_headers(),
    )
    assert r.status_code == 201, f"创建临时用户失败：{r.status_code} {r.text[:200]}"
    user_id = r.json()["id"]
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM kh_user_role WHERE user_id = :uid"), {"uid": int(user_id)}
        )
    return user_id, username


async def _cleanup_user(username: str) -> None:
    """物理删除临时用户（先子表后主表）"""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM kh_user_role WHERE user_id IN "
                "(SELECT id FROM kh_user WHERE username = :u)"
            ),
            {"u": username},
        )
        await conn.execute(text("DELETE FROM kh_user WHERE username = :u"), {"u": username})


async def cleanup() -> None:
    """物理清理 ES 测试文档（幂等）"""
    c = es_service.client
    if c is not None:
        for did in (DOC_A_ID, DOC_B_ID):
            try:
                await c.delete(index=DOCUMENT_INDEX, id=did, refresh=True)
            except Exception:
                pass


async def main() -> None:
    print("=== 全文搜索接口冒烟 ===")

    # 压掉 SQLAlchemy echo（import 后设置，避免淹没断言输出）
    engine.echo = False

    # 0) ES 自检
    await es_service.start()
    assert es_service.client is not None, "ES 客户端不可用，请检查 docker/elasticsearch"

    # 1) 写入两条测试文档（index_document 内部 refresh）
    await search_index_service.index_document(DOC_A)
    await search_index_service.index_document(DOC_B)
    print(f" 准备：写入 ES 测试文档 {DOC_A_ID} / {DOC_B_ID}")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 2) 未登录 → 401
        r = await client.post(f"{BASE}/search", json={"keyword": KEYWORD})
        check("未登录", r.status_code == 401, f"期望 401，实际 {r.status_code}")

        # 3) 无 search 权限的用户（角色清空 → 零权限码）→ 403
        nobody_id, nobody_name = await _create_roleless_user(client)
        try:
            r = await client.post(
                f"{BASE}/search",
                json={"keyword": KEYWORD},
                headers=bearer_headers(nobody_id, nobody_name),
            )
        finally:
            await _cleanup_user(nobody_name)
        check("无权限用户", r.status_code == 403, f"期望 403，实际 {r.status_code}")

        # 4) 管理员关键词搜索 → 200 + 高亮
        r = await client.post(
            f"{BASE}/search",
            json={"keyword": KEYWORD},
            headers=bearer_headers(),
        )
        check("管理员搜索", r.status_code == 200, f"期望 200，实际 {r.status_code} body={r.text[:200]}")
        body = r.json()
        assert body["total"] >= 2, f"应命中 2 条测试文档，实际 total={body['total']}"
        items = body["items"]
        # 高亮断言：至少一条的 highlight 含 <em> 包裹的关键词
        hit_highlight = False
        for it in items:
            hl = it.get("highlight") or {}
            flat = " ".join(frag for frags in hl.values() for frag in frags)
            if KEYWORD in flat and "<em>" in flat:
                hit_highlight = True
                break
        check(
            "关键词高亮",
            hit_highlight,
            "highlight 应包含 <em> 包裹的命中词" if not hit_highlight else "命中词被 <em> 包裹",
        )
        # 字段完整性：返回的命中项含标题 / score / highlight
        first = items[0]
        check(
            "返回字段",
            bool(first.get("title"))
            and "score" in first
            and first.get("highlight") is not None,
            f"title={first.get('title')!r}, score={first.get('score')!r}, highlight={first.get('highlight') is not None}",
        )

        # 4.2) 可见性字段回传：
        # DOC_A 带 teamId、DOC_B 无该字段 → 分别断言「有值」与「None」
        by_id = {it["id"]: it for it in items}
        a_item = by_id.get(str(DOC_A_ID), {})
        b_item = by_id.get(str(DOC_B_ID), {})
        check(
            "可见性字段 isPublic",
            a_item.get("isPublic") is True and b_item.get("isPublic") is True,
            f"A.isPublic={a_item.get('isPublic')!r}, B.isPublic={b_item.get('isPublic')!r}",
        )
        check(
            "可见性字段 teamId",
            a_item.get("teamId") == SMOKE_TEAM_ID and b_item.get("teamId") in (None, ""),
            f"A.teamId={a_item.get('teamId')!r}（期望 {SMOKE_TEAM_ID}）, "
            f"B.teamId={b_item.get('teamId')!r}（期望 None，字段缺失）",
        )

        # 4.5) ES 层验证：列表查询 _source 已排除 content 全文
        raw = await es_service.client.search(
            index=DOCUMENT_INDEX,
            query={"term": {"id": DOC_A_ID}},
            source={"excludes": ["content"]},
        )
        raw_src = raw["hits"]["hits"][0]["_source"]
        check(
            "ES 排除正文",
            "content" not in raw_src,
            f"_source 应不含 content，实际 keys={list(raw_src.keys())}",
        )

        # 5) 分类过滤：只命中 catA
        r = await client.post(
            f"{BASE}/search",
            json={"keyword": KEYWORD, "categoryId": CAT_A},
            headers=bearer_headers(),
        )
        body = r.json()
        check(
            "分类过滤",
            body["total"] == 1 and body["items"][0]["id"] == DOC_A_ID,
            f"categoryId={CAT_A} 应只命中 {DOC_A_ID}，实际 total={body['total']} ids={[i['id'] for i in body['items']]}",
        )

        # 6) 分页：pageSize=1 → 返回 1 条，但 total 不裁剪
        r = await client.post(
            f"{BASE}/search",
            json={"keyword": KEYWORD, "pageSize": 1},
            headers=bearer_headers(),
        )
        body = r.json()
        check(
            "分页",
            len(body["items"]) == 1 and body["total"] >= 2,
            f"pageSize=1 应返回 1 条，total 应 >=2；实际 len={len(body['items'])} total={body['total']}",
        )

    await cleanup()
    print("\n全文搜索接口冒烟全部通过 ✅")


async def run() -> None:
    """main + cleanup 同一事件循环（engine 连接池绑定首个 loop）"""
    try:
        await main()
    except Exception:
        print("冒烟测试失败，执行清理", file=sys.stderr)
        await cleanup()
        raise
    finally:
        await es_service.close()


if __name__ == "__main__":
    asyncio.run(run())
