"""chunk 级混合检索接口 E2E 冒烟（真实 PG + Mongo + ES，裸 FastAPI app + ASGITransport）

覆盖 /api/v1/search/hybrid（方案 B：ES kh_chunk 关键词 + PG doc_chunk 向量，RRF 融合）：
  1. 未登录 → 401
  2. 无 search 权限的用户（临时用户清空角色关联 → 零权限码）→ 403
  3. 创建已发布文档 → POST /{id}/index：PG doc_chunk（向量）+ ES kh_chunk（IK 关键词）双写
  4. ES 层验证 kh_chunk 已含该文档 chunk（双写落盘）
  5. 管理员混合检索唯一关键词 → 200 命中该文档 chunk；
     回归锁定 `_to_camel_row`：documentId/categoryId/authorId/teamId/chunkId 一律 str
     （对齐 TS bigintTransformer，防 JS Number 精度丢失），score 为 float，content 非空；
     两路都召回 → bm25Score / vectorScore 均 > 0（对齐 ES ChunkHit 通道原始分）
  6. 空串 query → 200 items=[]（service 空查询短路，不 500）
  7. 清理：ES kh_chunk（delete_by_query）→ PG 物理删文档（级联 doc_chunk）→ Mongo content
     → 临时无权限用户（kh_user_role + kh_user），零残留

注：403 用例不用种子 REVIEWER 测——9-3 补齐 11 行权限矩阵时 REVIEWER 也绑了 search 码
（init.sql 4100000000000000011 行），种子角色已无「无 search」者；改为临时用户清空角色。
smoke_search_api.py 同款修正。

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_search_hybrid.py

前置：docker 的 postgres / mongo / elasticsearch 已就绪（连接读 backend/.env）。
"""
import asyncio
import sys
import time

import httpx
from sqlalchemy import delete, text

from app.core.config import settings
from app.db.es import es_service
from app.db.mongo import get_document_content_collection
from app.db.session import engine
from app.main import app
from app.models.document import DocumentEntity
from app.services.es_index import CHUNK_INDEX
from scripts.smoke_auth import bearer_headers

BASE = "http://testserver/api/v1"

# 强制免审（创建即发布，走真实发布链路；审核链路由 smoke_document_review*.py 覆盖）
settings.DOCUMENT_REQUIRE_APPROVAL = "false"

# 唯一关键词：自造中文短语，IK 与向量（3-gram 重叠）双路稳定命中，杜绝撞库数据
KEYWORD = "混合检索冒烟令牌"
TITLE = "混合检索冒烟文档"
# 多段长正文（>500 字符，保证切成多块；每节都含 KEYWORD）
LONG_CONTENT = "\n\n".join(
    [f"## 第 {i} 节\n\n{KEYWORD}。" + ("企业知识库系统支持多模态文档的归档与语义检索。" * 60) for i in range(4)]
)

_results: list[str] = []


def check(step: str, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    _results.append(f"{mark} {step}: {detail}")
    if not ok:
        raise AssertionError(f"{step} 失败：{detail}")


async def _create_roleless_user(client: httpx.AsyncClient, tag: str) -> tuple[str, str]:
    """创建临时用户并清空其角色关联 → 零权限码（403 用例用，零污染由 cleanup 负责）

    CreateUserRequest.roleCodes 空数组会回退 ROLE_USER（对齐 TS），
    AssignRolesRequest.roleCodes 又 min_length=1 拒空数组，故建用户后直连 DB 删角色行。
    """
    username = f"{tag}{int(time.time() * 1000)}"
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
    """物理删除临时用户（先子表后主表，含潜在崩溃残留的软删行）"""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM kh_user_role WHERE user_id IN "
                "(SELECT id FROM kh_user WHERE username = :u)"
            ),
            {"u": username},
        )
        await conn.execute(text("DELETE FROM kh_user WHERE username = :u"), {"u": username})


async def _cleanup_es(doc_id: str) -> None:
    """ES kh_chunk 按 document_id 清理（幂等；索引不存在视为已清空）"""
    c = es_service.client
    if c is None:
        return
    try:
        await c.delete_by_query(
            index=CHUNK_INDEX,
            query={"term": {"document_id": doc_id}},
            refresh=True,
        )
    except Exception:
        pass


async def cleanup(doc_id: str | None) -> None:
    if doc_id is None:
        return
    await _cleanup_es(doc_id)
    async with engine.begin() as conn:
        # kh_document 物理删除 → doc_chunk FK ON DELETE CASCADE 级联清理
        await conn.execute(delete(DocumentEntity).where(DocumentEntity.id == int(doc_id)))
    await get_document_content_collection().delete_one({"documentId": str(doc_id)})
    print("   清理：ES kh_chunk + PG 文档（级联 doc_chunk）+ Mongo content 已移除")


async def main() -> None:
    print("=== chunk 级混合检索接口冒烟 ===")

    # 压掉 SQLAlchemy echo（import 后设置，避免淹没断言输出）
    engine.echo = False

    # 0) ES 自检（hybrid 关键词通道依赖）
    await es_service.start()
    assert es_service.client is not None, "ES 客户端不可用，请检查 docker/elasticsearch"

    doc_id: str | None = None
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1) 未登录 → 401
            r = await client.post(f"{BASE}/search/hybrid", json={"query": KEYWORD})
            check("未登录", r.status_code == 401, f"期望 401，实际 {r.status_code}")

            # 2) 无 search 权限的用户（角色清空 → 零权限码）→ 403
            nobody_id, nobody_name = await _create_roleless_user(client, "smokehybrid0")
            try:
                r = await client.post(
                    f"{BASE}/search/hybrid",
                    json={"query": KEYWORD},
                    headers=bearer_headers(nobody_id, nobody_name),
                )
            finally:
                await _cleanup_user(nobody_name)
            check("无权限用户", r.status_code == 403, f"期望 403，实际 {r.status_code}")

            # 3) 创建已发布文档（免审 status=1 → published）→ 索引双写
            r = await client.post(
                f"{BASE}/documents",
                json={"title": TITLE, "content": LONG_CONTENT, "status": 1},
                headers=bearer_headers(),
            )
            check("创建文档", r.status_code == 201, f"期望 201，实际 {r.status_code} body={r.text[:200]}")
            doc_id = r.json()["id"]
            print(f"   准备：文档 {doc_id} 已发布")

            r = await client.post(
                f"{BASE}/documents/{doc_id}/index",
                headers=bearer_headers(),
            )
            body = r.json()
            check(
                "索引双写",
                r.status_code == 200 and body.get("chunkCount", 0) >= 2,
                f"期望 200 chunkCount>=2，实际 {r.status_code} body={body}",
            )
            chunk_count = body["chunkCount"]
            print(f"   POST /index → chunkCount={chunk_count}")

            # 4) ES 层验证 kh_chunk 已落盘该文档 chunk（关键词通道数据就绪）
            raw = await es_service.client.search(
                index=CHUNK_INDEX,
                query={"term": {"document_id": doc_id}},
                size=0,
            )
            es_total = raw["hits"]["total"]["value"]
            check(
                "ES kh_chunk 落盘",
                es_total >= 2,
                f"kh_chunk 应含 >=2 个该文档 chunk，实际 {es_total}",
            )

            # 5) 混合检索：命中该文档 chunk；bigint 字段均为 str（_to_camel_row 回归）
            r = await client.post(
                f"{BASE}/search/hybrid",
                json={"query": KEYWORD, "topK": 10},
                headers=bearer_headers(),
            )
            check("混合检索", r.status_code == 200, f"期望 200，实际 {r.status_code} body={r.text[:200]}")
            items = r.json()["items"]
            hit = [it for it in items if it.get("documentId") == doc_id]
            check(
                "命中目标文档",
                len(hit) >= 1,
                f"应命中文档 {doc_id} 的 chunk，实际 documentIds={[i.get('documentId') for i in items]}",
            )
            first = hit[0]
            bigint_ok = all(
                first.get(k) is None or isinstance(first.get(k), str)
                for k in ("documentId", "chunkId", "categoryId", "authorId", "teamId")
            )
            # 通道原始分（对齐 ES ChunkHit）：命中文档的块两路都召回 → bm25/vector 均 > 0
            bm25, vec = first.get("bm25Score"), first.get("vectorScore")
            channel_ok = (
                isinstance(bm25, (int, float))
                and bm25 > 0
                and isinstance(vec, (int, float))
                and vec > 0
            )
            check(
                "bigint→str 回归",
                bigint_ok
                and isinstance(first.get("score"), (int, float))
                and bool(first.get("content"))
                and isinstance(first.get("chunkIndex"), int),
                f"chunkId={first.get('chunkId')!r} type={type(first.get('chunkId')).__name__}, "
                f"documentId={first.get('documentId')!r} type={type(first.get('documentId')).__name__}, "
                f"authorId={first.get('authorId')!r}, score={first.get('score')!r}, "
                f"chunkIndex={first.get('chunkIndex')!r}",
            )
            check(
                "通道原始分",
                channel_ok,
                f"bm25Score={bm25!r} type={type(bm25).__name__}, "
                f"vectorScore={vec!r} type={type(vec).__name__}",
            )

            # 6) 空串 query → 200 items=[]（service 空查询短路）
            r = await client.post(
                f"{BASE}/search/hybrid",
                json={"query": "   "},
                headers=bearer_headers(),
            )
            body = r.json()
            check(
                "空查询短路",
                r.status_code == 200 and body.get("items") == [],
                f"期望 200 items=[]，实际 {r.status_code} body={body}",
            )
    finally:
        await cleanup(doc_id)

    print("\nchunk 级混合检索冒烟全部通过 ✅")


async def run() -> None:
    """main + cleanup 同一事件循环（engine 连接池绑定首个 loop）"""
    try:
        await main()
    except Exception:
        print("冒烟测试失败，执行清理", file=sys.stderr)
        raise
    finally:
        await es_service.close()


if __name__ == "__main__":
    asyncio.run(run())
