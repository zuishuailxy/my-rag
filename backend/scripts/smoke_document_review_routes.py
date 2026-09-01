"""
文档审核路由冒烟测试（HTTP 层）

用不带 lifespan 的裸 FastAPI app 挂载文档路由，走真实 Postgres + MongoDB：
- 不连 RabbitMQ / ES / Neo4j，避免真的启动消费者去跑 KG 抽取（会调 LLM）
- 索引投递失败由 pipeline_publisher 兜底告警，不影响断言

覆盖：路由是否注册正确（尤其 reviews/tasks 是否被 /{doc_id} 吃掉）、
状态码（NestJS @Post 默认 201）、DTO 校验（422）、业务 400 / 404、
审核接口的 @Roles(REVIEWER, ADMIN) 守卫（无审核角色 → 403）、
审核人取自 JWT 而非请求体（ReviewDecision 只有 reviewComment）。

运行：PYTHONPATH=. .venv/bin/python scripts/smoke_document_review_routes.py
"""

import asyncio
import logging

import httpx
from bson import ObjectId
from fastapi import FastAPI
from sqlalchemy import text

from app.api.v1 import api_router
from app.core.config import settings
from app.db.mongo import close_mongo_client, get_document_content_collection
from app.db.session import async_session_factory
from smoke_auth import (
    SEED_ADMIN_ID,
    SEED_REVIEWER_ID,
    SEED_REVIEWER_USERNAME,
    SEED_USER_ID,
    SEED_USER_USERNAME,
    bearer_headers,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s [%(name)s] %(message)s",
)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

# 裸 app：不挂 lifespan，不连 MQ / ES / Neo4j
# 用 httpx.AsyncClient + ASGITransport（而非 TestClient）是为了让请求与数据库清理
# 共用同一个事件循环——TestClient 在独立线程跑循环，asyncpg 连接池会串线报错。
app = FastAPI()
app.include_router(api_router, prefix=settings.API_V1_PREFIX)

BASE = f"{settings.API_V1_PREFIX}/documents"


def show(step: str, resp: httpx.Response) -> None:
    """打印一步的结果（正文过长时截断）"""
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    print(f"{step:<52} {resp.status_code}  {str(body)[:110]}")


async def cleanup(doc_id: str | None, content_oid: ObjectId | None) -> None:
    """物理删除测试文档、审核流水与 Mongo 正文"""
    print("\n--- cleanup ---")
    if doc_id is not None:
        async with async_session_factory() as session:
            await session.execute(
                text("DELETE FROM kh_document_review WHERE document_id = :d"),
                {"d": int(doc_id)},
            )
            await session.execute(
                text("DELETE FROM kh_document WHERE id = :d"), {"d": int(doc_id)}
            )
            await session.commit()
    if content_oid is not None:
        await get_document_content_collection().delete_one({"_id": content_oid})
    print("已物理删除测试文档与审核流水")


async def main() -> None:
    doc_id: str | None = None
    content_oid: ObjectId | None = None
    ok = True

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://smoke",
        headers=bearer_headers(),
    ) as client:
        try:
            # 1) 创建草稿
            r = await client.post(
                BASE,
                json={"title": "路由冒烟-审核", "content": "审核流程路由测试正文"},
            )
            show("1) POST /documents（创建草稿）", r)
            ok &= r.status_code == 201
            doc_id = r.json()["id"]
            content_oid = ObjectId(r.json()["contentId"])

            # 2) 提交审核 → 201 + status=3
            # 注意：submit / approve / reject 返回的都是「文档」而非审核任务
            # （与 TS submitForReview / approveReview 返回 DocumentEntity 一致），
            # 因此任务 ID 必须从 GET /{id}/reviews/current 取。
            r = await client.post(f"{BASE}/{doc_id}/reviews/submit")
            show("2) POST /{id}/reviews/submit", r)
            ok &= r.status_code == 201 and r.json()["status"] == 3
            ok &= r.json()["id"] == doc_id  # 返回的是文档本身

            # 3) 当前待审记录（路由未被 /{doc_id} 吃掉）
            r = await client.get(f"{BASE}/{doc_id}/reviews/current")
            show("3) GET /{id}/reviews/current", r)
            ok &= r.status_code == 200 and r.json()["reviewResult"] is None
            ok &= r.json()["documentId"] == doc_id
            task_id = r.json()["id"]

            # 4) 待审数量（直接返回数字）
            r = await client.get(f"{BASE}/reviews/tasks/pending-count")
            show("4) GET /reviews/tasks/pending-count", r)
            ok &= r.status_code == 200 and isinstance(r.json(), int)

            # 5) 任务列表（默认 pending）
            r = await client.get(f"{BASE}/reviews/tasks")
            show("5) GET /reviews/tasks（默认 pending）", r)
            ok &= r.status_code == 200 and r.json()["pageSize"] == 20

            # 6) 非法 status → 422
            r = await client.get(f"{BASE}/reviews/tasks?status=foo")
            show("6) GET /reviews/tasks?status=foo", r)
            ok &= r.status_code == 422

            # 7) pageSize 越界 → 422
            r = await client.get(f"{BASE}/reviews/tasks?pageSize=101")
            show("7) GET /reviews/tasks?pageSize=101", r)
            ok &= r.status_code == 422

            # 8) 驳回但无意见 → 400（服务层校验，非 422）
            r = await client.post(f"{BASE}/reviews/tasks/{task_id}/reject", json={})
            show("8) POST reject（空意见）", r)
            ok &= r.status_code == 400

            # 9) 驳回带意见 → 201 + status=0
            #    请求体只有 reviewComment（TS ReviewDecisionDto 同款），
            #    请求体里夹带 reviewerName 会被 Pydantic 忽略（extra=ignore）。
            r = await client.post(
                f"{BASE}/reviews/tasks/{task_id}/reject",
                json={"reviewComment": "需补充来源", "reviewerName": "伪造的审核人"},
            )
            show("9) POST reject（带意见）", r)
            ok &= r.status_code == 201 and r.json()["status"] == 0

            # 10) 审核历史：审核人必须来自 JWT（admin / 系统管理员），不是请求体
            r = await client.get(f"{BASE}/{doc_id}/reviews/history")
            show("10) GET /{id}/reviews/history", r)
            ok &= r.status_code == 200 and r.json()[0]["reviewResult"] == 2
            ok &= r.json()[0]["reviewerId"] == SEED_ADMIN_ID
            ok &= r.json()[0]["reviewerName"] == "系统管理员"  # realName ?? username
            print(
                "   审核人来自 JWT: reviewerId =",
                r.json()[0]["reviewerId"],
                "| reviewerName =",
                r.json()[0]["reviewerName"],
            )

            # 11) 再次提审，并从 current 取新的任务 ID
            r = await client.post(f"{BASE}/{doc_id}/reviews/submit")
            show("11) POST /{id}/reviews/submit（二次）", r)
            ok &= r.status_code == 201 and r.json()["status"] == 3
            r = await client.get(f"{BASE}/{doc_id}/reviews/current")
            task_id = r.json()["id"]

            # 12) 审核通过 → 201 + status=1（审核人同样取自 JWT）
            r = await client.post(
                f"{BASE}/reviews/tasks/{task_id}/approve",
                json={"reviewComment": "通过", "reviewerId": "9001"},
            )
            show("12) POST approve", r)
            ok &= r.status_code == 201 and r.json()["status"] == 1
            r = await client.get(f"{BASE}/{doc_id}/reviews/history")
            ok &= r.json()[0]["reviewerId"] == SEED_ADMIN_ID  # 请求体的 9001 被忽略
            ok &= r.json()[0]["reviewerName"] == "系统管理员"

            # 13) 重复审核 → 400
            r = await client.post(f"{BASE}/reviews/tasks/{task_id}/approve", json={})
            show("13) POST approve（重复）", r)
            ok &= r.status_code == 400

            # 14) 不存在的审核任务 → 404
            r = await client.post(f"{BASE}/reviews/tasks/1/approve", json={})
            show("14) POST approve（不存在）", r)
            ok &= r.status_code == 404

            # 15) 结案后 current → null
            r = await client.get(f"{BASE}/{doc_id}/reviews/current")
            show("15) GET /{id}/reviews/current（结案后）", r)
            ok &= r.status_code == 200 and r.json() is None

            # 16) 无审核角色（ROLE_USER）→ 403（@Roles(REVIEWER, ADMIN) 守卫）
            #     403 发生在业务逻辑之前：这里 task_id 已结案，若守卫漏了会返回 400 而非 403
            wrong_role = bearer_headers(SEED_USER_ID, SEED_USER_USERNAME)
            r = await client.get(f"{BASE}/reviews/tasks/pending-count", headers=wrong_role)
            show("16) GET pending-count（ROLE_USER）", r)
            ok &= r.status_code == 403

            r = await client.post(
                f"{BASE}/reviews/tasks/{task_id}/approve", json={}, headers=wrong_role
            )
            show("    POST approve（ROLE_USER）", r)
            ok &= r.status_code == 403

            # 17) ROLE_REVIEWER → 放行（列表接口返回 200）
            reviewer_role = bearer_headers(SEED_REVIEWER_ID, SEED_REVIEWER_USERNAME)
            r = await client.get(f"{BASE}/reviews/tasks", headers=reviewer_role)
            show("17) GET /reviews/tasks（ROLE_REVIEWER）", r)
            ok &= r.status_code == 200

        finally:
            await cleanup(doc_id, content_oid)
            await close_mongo_client()

    print("\n文档审核路由冒烟测试" + ("全部通过 ✅" if ok else "存在失败 ❌"))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
