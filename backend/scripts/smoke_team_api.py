"""团队模块 E2E 冒烟

覆盖 TeamController 已接线端点（POST / PUT / DELETE / page / mine / tree /
:id / members 三件套）：
  401 匿名 → 403 普通用户 → **/mine 仅需登录（非 ADMIN 也 200）** →
  **/tree 收紧为 ADMIN（匿名 401 / USER 403）** → 创建（默认值 / 全字段 /
  同码不查重 201）→ page（keyword / status / pageSize）→ tree（嵌套 / rootOnly）→
  详情 memberCount → 404（不存在 / 非法 id）→ 父不能是自己 400 →
  部分更新 → members 添加（幂等 / 404 / 空数组）→ 成员列表 →
  members 移除 → 有子团队 400 → 删除叶子（成员硬删）→ 重复删 404 →
  tree 排除已删 → **/mine 排除已软删团队**（listAccessibleTeamIds 的 deleted 过滤）。

种子团队只读（kh_team 2 行 / kh_team_member 3 行为 init.sql 预置）；
临时团队 + 临时成员关联结束硬删（先子表后主表），断言 kh_team 回到 2 行。
运行：cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_team_api.py
"""

import asyncio
import secrets
import time

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from app.api.v1 import api_router
from app.core.config import settings
from app.db.session import async_session_factory, engine
from smoke_auth import SEED_ADMIN_ID, SEED_USER_ID, bearer_headers

logging_import = __import__("logging")
logging_import.basicConfig(
    level=logging_import.INFO, format="%(levelname)s [%(name)s] %(message)s"
)
engine.echo = False

app = FastAPI()
app.include_router(api_router, prefix=settings.API_V1_PREFIX)
API = settings.API_V1_PREFIX

RUN_TAG = f"{int(time.time())}{secrets.token_hex(3)}"
# 种子团队：8000000000000000001 技术中心（根）/ 8000000000000000002 后端开发组（其子）
SEED_ROOT_TEAM_ID = "8000000000000000001"
SEED_BACKEND_TEAM_ID = "8000000000000000002"

_results: list[str] = []
CREATED_TEAM_IDS: list[str] = []


def check(step: object, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    _results.append(f"{mark} {step:>4} {detail}")


async def cleanup() -> None:
    """硬删临时团队（先 kh_team_member 子表，再 kh_team 主表），并核对种子行仍在

    ⚠️ 断言口径：只验「本次创建的临时团队已清空 + 种子行完好」，**不断言 kh_team 全局
    行数**——库里有 2026-09-03 手工测试留下的团队（前端开发组 ×2 + 一条 xiao 的成员
    关联），全局计数既脆弱又会误报（对齐项目约定：断言不依赖总行数这类脆弱前提）。
    """
    ids = [int(i) for i in CREATED_TEAM_IDS]
    if ids:
        async with async_session_factory() as session:
            await session.execute(
                text("DELETE FROM kh_team_member WHERE team_id = ANY(:ids)"), {"ids": ids}
            )
            await session.execute(
                text("DELETE FROM kh_team WHERE id = ANY(:ids)"), {"ids": ids}
            )
            await session.commit()
            print(f"🧹 已删除临时团队 {len(CREATED_TEAM_IDS)} 个（含成员关联）")
    seed_team_ids = [int(i) for i in (SEED_ROOT_TEAM_ID, SEED_BACKEND_TEAM_ID)]
    seed_member_ids = [9000000000000000001, 9000000000000000002, 9000000000000000003]
    async with async_session_factory() as session:
        left = (
            await session.execute(
                text("SELECT count(*) FROM kh_team WHERE id = ANY(:ids)"), {"ids": ids}
            )
        ).scalar_one() if ids else 0
        n_seed_team = (
            await session.execute(
                text("SELECT count(*) FROM kh_team WHERE id = ANY(:ids)"),
                {"ids": seed_team_ids},
            )
        ).scalar_one()
        n_seed_member = (
            await session.execute(
                text("SELECT count(*) FROM kh_team_member WHERE id = ANY(:ids)"),
                {"ids": seed_member_ids},
            )
        ).scalar_one()
        check(
            "清理",
            left == 0 and n_seed_team == 2 and n_seed_member == 3,
            f"临时团队残留 {left}（应 0）；种子 kh_team={n_seed_team}/2 "
            f"kh_team_member={n_seed_member}/3",
        )


async def main() -> None:
    user_headers = bearer_headers(SEED_USER_ID, "user")
    AUTH = bearer_headers()

    # 1. 未登录 401（/page 与 /tree 都是 ADMIN 限定；/mine 仅需登录）
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://smoke"
    ) as anon:
        r = await anon.get(f"{API}/teams/page")
        r_mine = await anon.get(f"{API}/teams/mine")
        r_tree = await anon.get(f"{API}/teams/tree")
    check(1, r.status_code == 401, f"无 token GET /teams/page → {r.status_code}")
    check(1.1, r_mine.status_code == 401, f"无 token GET /teams/mine → {r_mine.status_code}")
    check(
        1.2,
        r_tree.status_code == 401,
        f"无 token GET /teams/tree → {r_tree.status_code}",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://smoke",
        headers=AUTH,
    ) as client:
        # 2. 普通用户 403（@roles(ADMIN)）
        r = await client.get(f"{API}/teams/page", headers=user_headers)
        check(2, r.status_code == 403, f"USER 角色 GET /teams/page → {r.status_code}")

        # 2.1 /tree 同样 ADMIN 限定（(ADMIN) + @RequirePermission('system:team')）
        r = await client.get(f"{API}/teams/tree", headers=user_headers)
        check(
            2.1,
            r.status_code == 403,
            f"USER 角色 GET /teams/tree → {r.status_code}",
        )

        # 2.2 /mine 登录即可：
        r = await client.get(f"{API}/teams/mine", headers=user_headers)
        mine_user = [t["id"] for t in r.json()] if r.status_code == 200 else []
        # 种子：user 只在「后端开发组」成员表里（技术中心的成员是 admin）
        check(
            2.2,
            r.status_code == 200 and mine_user == [SEED_BACKEND_TEAM_ID],
            f"USER GET /teams/mine → {r.status_code} 团队={mine_user}（登录即可，非 ADMIN）",
        )

        # 2.3 /mine 对 admin 走「负责人」分支（两行种子团队 leader_id 都是 admin）
        r = await client.get(f"{API}/teams/mine")
        mine_admin = {t["id"] for t in r.json()} if r.status_code == 200 else set()
        check(
            2.3,
            r.status_code == 200
            and {SEED_ROOT_TEAM_ID, SEED_BACKEND_TEAM_ID} <= mine_admin,
            f"ADMIN GET /teams/mine → {r.status_code} 含两个种子团队（成员 ∪ 负责人）",
        )

        # 3. 最小字段创建 → 201 + 默认值（parentId '0' / sort 0 / status 1）
        r = await client.post(f"{API}/teams", json={"teamName": f"冒烟团队{RUN_TAG}"})
        body = r.json()
        root_id = body.get("id")
        check(
            3,
            r.status_code == 201
            and body["parentId"] == "0"
            and body["sort"] == 0
            and body["status"] == 1
            and body["teamCode"] is None
            and body["leaderId"] is None
            and body["deleted"] is False,
            f"POST /teams 最小字段 → {r.status_code} 默认值 parentId={body.get('parentId')}",
        )
        if root_id:
            CREATED_TEAM_IDS.append(root_id)

        # 4. 全字段创建（父 = 种子技术中心，负责人 = admin）
        r = await client.post(
            f"{API}/teams",
            json={
                "teamName": f"冒烟子团队{RUN_TAG}",
                "teamCode": f"SMOKE_{RUN_TAG}",
                "description": "冒烟子团队",
                "parentId": SEED_ROOT_TEAM_ID,
                "leaderId": SEED_ADMIN_ID,
                "sort": 5,
                "status": 1,
            },
        )
        body = r.json()
        child_id = body.get("id")
        check(
            4,
            r.status_code == 201
            and body["parentId"] == SEED_ROOT_TEAM_ID
            and body["leaderId"] == SEED_ADMIN_ID
            and body["sort"] == 5,
            f"POST /teams 全字段 → {r.status_code} parentId/leaderId 以字符串回显",
        )
        if child_id:
            CREATED_TEAM_IDS.append(child_id)

        # 如实 201（保真：不引入 409）
        r = await client.post(
            f"{API}/teams",
            json={"teamName": f"冒烟团队{RUN_TAG}", "teamCode": f"SMOKE_{RUN_TAG}"},
        )
        dup_id = r.json().get("id") if r.status_code == 201 else None
        check(5, r.status_code == 201, f"POST /teams 同码不查重 → {r.status_code}")
        if dup_id:
            CREATED_TEAM_IDS.append(dup_id)

        # 6. page keyword 模糊 + pageSize 生效
        r = await client.get(
            f"{API}/teams/page", params={"keyword": RUN_TAG, "pageSize": 2}
        )
        body = r.json()
        check(
            6,
            r.status_code == 200
            and body["total"] == len(CREATED_TEAM_IDS)
            and len(body["items"]) == 2
            and body["pageSize"] == 2,
            f"page keyword={RUN_TAG} → total={body.get('total')} 本页 {len(body.get('items', []))} 条",
        )

        # 7. page status 过滤（先全部 1，再禁用 dup）
        r = await client.put(
            f"{API}/teams/{dup_id}", json={"status": 0}
        ) if dup_id else None
        r2 = await client.get(
            f"{API}/teams/page", params={"keyword": RUN_TAG, "status": 0}
        )
        zero_ids = [i["id"] for i in r2.json().get("items", [])]
        check(7, r2.status_code == 200 and zero_ids == ([dup_id] if dup_id else []),
              f"page status=0 → 命中 {len(zero_ids)} 条（应为禁用的 dup）")

        # 8. tree 嵌套：种子技术中心的 children 含我们的子团队
        r = await client.get(f"{API}/teams/tree")
        tree = r.json()
        root_map = {n["id"]: n for n in tree}
        child_names = [
            c["teamName"] for c in root_map.get(SEED_ROOT_TEAM_ID, {}).get("children", [])
        ]
        check(
            8,
            r.status_code == 200
            and any(f"冒烟子团队{RUN_TAG}" in n for n in child_names),
            f"tree 嵌套 → 种子根的 children 含子团队（{child_names}）",
        )

        # 9. tree rootOnly=true → 只有根节点、children 一律空
        r = await client.get(f"{API}/teams/tree", params={"rootOnly": "true"})
        nodes = r.json()
        root_only_ok = r.status_code == 200 and all(
            n["parentId"] == "0" and n["children"] == [] for n in nodes
        )
        check(9, root_only_ok, f"tree rootOnly → {len(nodes)} 个根节点且 children 均空")

        # 10. 详情 memberCount=0
        r = await client.get(f"{API}/teams/{child_id}")
        body = r.json()
        check(10, r.status_code == 200 and body.get("memberCount") == 0,
              f"GET /teams/{{id}} → {r.status_code} memberCount={body.get('memberCount')}")

        # 11/12. 404：合法但不存在 / 非法 id
        r = await client.get(f"{API}/teams/999999999999999999")
        check(11, r.status_code == 404 and r.json()["detail"] == "团队不存在",
              f"GET 假 id → {r.status_code} 团队不存在")
        r = await client.get(f"{API}/teams/abc")
        check(12, r.status_code == 404, f"GET 非法 id → {r.status_code}")

        # 13. 父团队不能是自己 → 400
        r = await client.put(f"{API}/teams/{child_id}", json={"parentId": child_id})
        check(13, r.status_code == 400 and r.json()["detail"] == "父团队不能是自己",
              f"PUT parentId=自身 → {r.status_code}")

        # 14. 部分更新：改 status + 空串清空 description
        r = await client.put(
            f"{API}/teams/{child_id}", json={"status": 0, "description": ""}
        )
        body = r.json()
        check(
            14,
            r.status_code == 200 and body["status"] == 0 and body["description"] == "",
            f"PUT 部分更新 → {r.status_code} status={body.get('status')}",
        )

        # 15. PUT 假 id → 404
        r = await client.put(f"{API}/teams/999999999999999999", json={"status": 1})
        check(15, r.status_code == 404, f"PUT 假 id → {r.status_code}")

        # 16. 加成员（种子 user）→ 201 true；重复添加幂等
        r = await client.post(f"{API}/teams/{child_id}/members", json=[SEED_USER_ID])
        check(16, r.status_code == 201 and r.json() is True,
              f"POST members → {r.status_code} body={r.text}")
        r = await client.post(f"{API}/teams/{child_id}/members", json=[SEED_USER_ID])
        check(16.1, r.status_code == 201, f"POST members 重复 → {r.status_code}（幂等跳过）")

        # 17. 部分用户不存在 → 404（合法但不存在的雪花）
        r = await client.post(
            f"{API}/teams/{child_id}/members", json=["999999999999999999"]
        )
        check(17, r.status_code == 404 and r.json()["detail"] == "部分用户不存在",
              f"POST members 假用户 → {r.status_code}")

        # 18. 空数组 → 201 true no-op（([]) 同款）
        r = await client.post(f"{API}/teams/{child_id}/members", json=[])
        check(18, r.status_code == 201 and r.json() is True, f"POST members 空数组 → {r.status_code}")

        # 19. 成员列表 + memberCount=1
        r = await client.get(f"{API}/teams/{child_id}/members")
        members = r.json()
        r2 = await client.get(f"{API}/teams/{child_id}")
        check(
            19,
            r.status_code == 200
            and len(members) == 1
            and members[0]["userId"] == SEED_USER_ID
            and members[0]["memberRole"] == "member"
            and members[0]["username"] == "user"
            and r2.json()["memberCount"] == 1,
            f"GET members → 1 行 userId={members[0]['userId'] if members else '?'} "
            f"memberCount={r2.json().get('memberCount')}",
        )

        # 20. 移除成员 → 成员清空、memberCount 归 0
        # ⚠️ httpx 的 delete() 不接受 json 参数，DELETE 带请求体须用 request()
        r = await client.request(
            "DELETE", f"{API}/teams/{child_id}/members", json=[SEED_USER_ID]
        )
        r2 = await client.get(f"{API}/teams/{child_id}")
        check(20, r.status_code == 200 and r.json() is True and r2.json()["memberCount"] == 0,
              f"DELETE members → {r.status_code} memberCount={r2.json().get('memberCount')}")

        # 21. 有子团队的种子根 → 400（种子后端开发组是技术中心的子）
        r = await client.delete(f"{API}/teams/{SEED_ROOT_TEAM_ID}")
        check(21, r.status_code == 400 and r.json()["detail"] == "存在子团队，无法删除",
              f"DELETE 有子团队 → {r.status_code}")

        # 22. 删叶子团队 → 软删 + 成员硬删；详情 / 成员列表随后 404
        r = await client.delete(f"{API}/teams/{child_id}")
        check(22, r.status_code == 200 and r.json() == {"message": "删除成功"},
              f"DELETE 叶子 → {r.status_code} {r.text}")
        r2 = await client.get(f"{API}/teams/{child_id}")
        r3 = await client.get(f"{API}/teams/{child_id}/members")
        check(22.1, r2.status_code == 404 and r3.status_code == 404,
              f"删后 GET 详情/成员 → {r2.status_code}/{r3.status_code}")

        # 23. 重复删 → 404
        r = await client.delete(f"{API}/teams/{child_id}")
        check(23, r.status_code == 404, f"DELETE 重复 → {r.status_code}")

        # 24. 删后的团队不再出现在 tree（status=0 本就不出现，软删同样排除）
        r = await client.get(f"{API}/teams/tree")
        gone = all(
            c["id"] != child_id
            for n in r.json()
            for c in n.get("children", [])
        )
        check(24, gone, "tree 不再含已删团队")

        # 25. /mine 的软删过滤
        # 新建临时团队 + 把 user 加进去 → 应出现在 user 的 /mine
        r = await client.post(
            f"{API}/teams", json={"teamName": f"冒烟mine团队{RUN_TAG}"}
        )
        mine_team_id = r.json().get("id") if r.status_code == 201 else None
        if mine_team_id:
            CREATED_TEAM_IDS.append(mine_team_id)
        r2 = (
            await client.post(
                f"{API}/teams/{mine_team_id}/members", json=[SEED_USER_ID]
            )
            if mine_team_id
            else None
        )
        r3 = await client.get(f"{API}/teams/mine", headers=user_headers)
        ids_before = [t["id"] for t in r3.json()] if r3.status_code == 200 else []
        check(
            25,
            r.status_code == 201
            and r2 is not None
            and r2.status_code == 201
            and mine_team_id in ids_before,
            f"/mine 含刚加入的团队（user 可见 {len(ids_before)} 个）",
        )

        # 26. 软删该团队后再查 /mine → 必须消失
        # ⚠️ 这正是不加 deleted 过滤的后果：已删团队会继续授予文档可见性
        r = await client.delete(f"{API}/teams/{mine_team_id}")
        r2 = await client.get(f"{API}/teams/mine", headers=user_headers)
        ids_after = [t["id"] for t in r2.json()] if r2.status_code == 200 else []
        check(
            26,
            r.status_code == 200 and mine_team_id not in ids_after,
            f"软删团队不再出现在 /mine（删前 {len(ids_before)} → 删后 {len(ids_after)}）",
        )


if __name__ == "__main__":
    async def run_all() -> None:
        try:
            await main()
        finally:
            await cleanup()

    asyncio.run(run_all())
    print()
    for line in _results:
        print(line)
    failed = [line for line in _results if line.startswith("❌")]
    print(f"\n{'全部 ' + str(len(_results)) + ' 步通过 ✅' if not failed else f'失败 {len(failed)} 步 ❌'}")
    raise SystemExit(1 if failed else 0)
