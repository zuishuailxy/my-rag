"""
bigint ID 字段校验冒烟测试（HTTP 层）

背景：PATCH /documents/{id} 传 Swagger 默认示例值 categoryId="string" 时，
旧实现走裸 int() 抛 ValueError → 500。现统一改为 app.common.bigint.require_bigint，
非法 / 超 int64 一律 400「Invalid <field>: <value>」。

覆盖：
- PATCH / POST / GET 列表三类入口的非法 ID → 400（不是 500、也不是静默忽略）
- 超 int64 数字串 → 400（PG BIGINT 上限，旧实现能过校验但会在 commit 时炸）
- 合法值 → 正常生效；空串 → 保持原语义（请求体清空字段 / 查询参数不筛选）
- 校验前置：PATCH 同时带正文与非法 ID 时，Mongo 正文不得被改动（version 不递增）
- POST 非法 teamId 不得留下 Mongo 孤儿正文
- createBy / authorId / updateBy **不参与** bigint 校验：服务层一律忽略 DTO 的这三个字段
  （作者 / 创建人 / 最后修改人恒取 JWT 用户），传非法值既不 400 也不改变落库值
- 审核接口的审核人取自 JWT：请求体里夹带 reviewerId 一律被忽略（Pydantic extra=ignore），
  落库的 reviewerId 是当前登录用户，伪造不了

用裸 FastAPI app（不挂 lifespan），走真实 Postgres + MongoDB；
全程不触发 publish / approve 成功路径，因此不会投递 MQ、不消耗 LLM token。

运行：PYTHONPATH=. .venv/bin/python scripts/smoke_document_bigint_validation.py
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
from smoke_auth import SEED_ADMIN_ID, bearer_headers

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s [%(name)s] %(message)s",
)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

# 裸 app：不挂 lifespan，不连 MQ / ES / Neo4j
app = FastAPI()
app.include_router(api_router, prefix=settings.API_V1_PREFIX)

BASE = f"{settings.API_V1_PREFIX}/documents"

# PostgreSQL BIGINT 上限 + 1，用于验证越界拦截
OVER_INT64 = "9223372036854775808"


def show(step: str, resp: httpx.Response) -> None:
    """打印一步的结果（正文过长时截断）"""
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    print(f"{step:<56} {resp.status_code}  {str(body)[:100]}")


async def mongo_version(oid: ObjectId) -> int:
    """读取 Mongo 正文的 version（用于验证正文是否被改动过）"""
    doc = await get_document_content_collection().find_one({"_id": oid})
    return int(doc["version"]) if doc else -1


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
            # 1) 创建草稿（合法 ID 字段）
            r = await client.post(
                BASE,
                json={
                    "title": "bigint 校验冒烟",
                    "content": "原始正文",
                    "categoryId": "1001",
                    "createBy": "9001",
                },
            )
            show("1) POST /documents（合法 ID）", r)
            ok &= r.status_code == 201 and r.json()["categoryId"] == "1001"
            # 请求体里的 createBy=9001 被忽略：创建人恒取 JWT 用户
            ok &= r.json()["createBy"] == SEED_ADMIN_ID
            doc_id = r.json()["id"]
            content_oid = ObjectId(r.json()["contentId"])
            base_version = await mongo_version(content_oid)

            # 2) 复现原始 bug：categoryId="string" → 400（旧实现 500）
            r = await client.patch(f"{BASE}/{doc_id}", json={"categoryId": "string"})
            show("2) PATCH categoryId=string", r)
            ok &= r.status_code == 400
            ok &= r.json()["detail"] == "Invalid categoryId: string"

            # 3) teamId 非数字 → 400
            r = await client.patch(f"{BASE}/{doc_id}", json={"teamId": "abc"})
            show("3) PATCH teamId=abc", r)
            ok &= r.status_code == 400 and "teamId" in r.json()["detail"]

            # 4) updateBy / authorId **已不参与校验**：服务层忽略 DTO 的这两个字段
            #    （updateBy 恒取 JWT 操作者；authorId 不可改）→ 传非法值既不 400，
            #    也不改变落库值。这是与 TS 对齐后的刻意行为。
            r = await client.patch(f"{BASE}/{doc_id}", json={"updateBy": "12.5"})
            show("4) PATCH updateBy=12.5（DTO 字段已忽略）", r)
            ok &= r.status_code == 200 and r.json()["updateBy"] == SEED_ADMIN_ID

            r = await client.patch(f"{BASE}/{doc_id}", json={"authorId": OVER_INT64})
            show("   PATCH authorId 超 int64（DTO 字段已忽略）", r)
            ok &= r.status_code == 200 and r.json()["authorId"] == SEED_ADMIN_ID

            # 5) categoryId 超 int64 → 400（旧实现能过 int()，但 commit 时 PG 报错）
            r = await client.patch(f"{BASE}/{doc_id}", json={"categoryId": OVER_INT64})
            show("5) PATCH categoryId 超 int64", r)
            ok &= r.status_code == 400 and "categoryId" in r.json()["detail"]

            # 6) 校验前置：带正文 + 非法 ID → 400，且 Mongo 正文不得被改
            r = await client.patch(
                f"{BASE}/{doc_id}",
                json={"content": "不该被写入的新正文", "categoryId": "bad"},
            )
            show("6) PATCH content + 非法 categoryId", r)
            ok &= r.status_code == 400
            ok &= await mongo_version(content_oid) == base_version
            r = await client.get(f"{BASE}/{doc_id}")
            ok &= r.json()["content"] == "原始正文"
            print(f"   → Mongo version 仍为 {base_version}，正文未被改动 ✅")

            # 7) 合法值正常生效
            r = await client.patch(
                f"{BASE}/{doc_id}", json={"categoryId": "2002", "teamId": "3003"}
            )
            show("7) PATCH 合法 categoryId/teamId", r)
            ok &= r.status_code == 200
            ok &= r.json()["categoryId"] == "2002" and r.json()["teamId"] == "3003"

            # 8) 空串 → 清空字段（保持改动前语义）
            r = await client.patch(f"{BASE}/{doc_id}", json={"categoryId": ""})
            show("8) PATCH categoryId=空串（清空）", r)
            ok &= r.status_code == 200 and r.json()["categoryId"] is None

            # 9) POST 非法 teamId → 400，且不留 Mongo 孤儿正文
            #    （createBy / authorId 已不参与校验：服务层忽略 DTO 的这两个字段）
            before = await get_document_content_collection().count_documents({})
            r = await client.post(
                BASE,
                json={"title": "不该建成", "content": "正文", "teamId": "string"},
            )
            show("9) POST teamId=string", r)
            ok &= r.status_code == 400 and "teamId" in r.json()["detail"]
            after = await get_document_content_collection().count_documents({})
            ok &= before == after
            print(f"   → Mongo 正文数 {before} → {after}，无孤儿 ✅")

            # 10) POST 非法 categoryId → 400（同样先校验后写 Mongo）
            r = await client.post(
                BASE,
                json={"title": "不该建成", "content": "正文", "categoryId": "string"},
            )
            show("10) POST categoryId=string", r)
            ok &= r.status_code == 400
            ok &= await get_document_content_collection().count_documents({}) == before

            # 11) 列表查询非法 categoryId → 400
            r = await client.get(f"{BASE}?categoryId=string")
            show("11) GET ?categoryId=string", r)
            ok &= r.status_code == 400 and "categoryId" in r.json()["detail"]

            # 12) 列表查询空串 → 200（真值判断：视为未筛选，不是筛 IS NULL）
            r = await client.get(f"{BASE}?categoryId=")
            show("12) GET ?categoryId=（空串不筛选）", r)
            ok &= r.status_code == 200 and r.json()["total"] >= 1

            # 13) 列表查询超 int64 → 400
            r = await client.get(f"{BASE}?teamId={OVER_INT64}")
            show("13) GET ?teamId 超 int64", r)
            ok &= r.status_code == 400

            # 14) 提审 → 201 + status=3
            r = await client.post(f"{BASE}/{doc_id}/reviews/submit")
            show("14) POST /{id}/reviews/submit", r)
            ok &= r.status_code == 201 and r.json()["status"] == 3
            task_id = (await client.get(f"{BASE}/{doc_id}/reviews/current")).json()["id"]

            # 15) 请求体夹带非法 reviewerId → 被忽略（不再走 require_bigint，也就不 400）
            #     审核人一律取 JWT，前端伪造不了审核身份
            r = await client.post(
                f"{BASE}/reviews/tasks/{task_id}/reject",
                json={"reviewComment": "补充来源", "reviewerId": "string"},
            )
            show("15) POST reject（夹带 reviewerId=string）", r)
            ok &= r.status_code == 201 and r.json()["status"] == 0

            # 16) 结案后 current → null；历史首条的审核人是 JWT 用户而非请求体
            r = await client.get(f"{BASE}/{doc_id}/reviews/current")
            show("16) GET /{id}/reviews/current（结案后）", r)
            ok &= r.status_code == 200 and r.json() is None
            r = await client.get(f"{BASE}/{doc_id}/reviews/history")
            ok &= r.status_code == 200 and r.json()[0]["reviewerId"] == SEED_ADMIN_ID
            print("   历史首条 reviewerId =", r.json()[0]["reviewerId"], "（来自 JWT）")

            # 17) 驳回意见必填仍在服务层生效（不是 DTO 层的 bigint 校验，但同属请求体之外的校验）
            r = await client.post(f"{BASE}/{doc_id}/reviews/submit")
            task_id = (await client.get(f"{BASE}/{doc_id}/reviews/current")).json()["id"]
            r = await client.post(
                f"{BASE}/reviews/tasks/{task_id}/reject", json={}
            )
            show("17) POST reject（空意见）", r)
            ok &= r.status_code == 400 and "驳回意见" in r.json()["detail"]

        finally:
            await cleanup(doc_id, content_oid)
            await close_mongo_client()

    print("\nbigint ID 校验冒烟测试" + ("全部通过 ✅" if ok else "存在失败 ❌"))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
