"""角色管理接口 E2E 冒烟（对应 TS RoleController / RoleService）

覆盖：
  - 类级 @Roles(ADMIN) → 全部接口：无 token 401、user/reviewer 403、admin 放行
  - GET  /roles/list        启用角色升序、不含 status 键、不含已禁用角色
  - GET  /roles/{id}        详情含 status；不存在/非法 id → 404「角色不存在」
  - POST /roles             201 不含 status 键；roleCode 重复 409
  - PUT  /roles/{id}        部分更新（含禁用后从 list 消失的联动断言）
  - DELETE /roles/{id}      有关联用户 400；解绑后 200「删除成功」；再查 404
  - GET  /roles/{id}/permissions   细粒度守卫 system:role（reviewer 403 / admin 200）
  - PUT  /roles/{id}/permissions   全量替换角色权限（空数组清空、再授予、假 id 404）

种子角色（admin/reviewer/user）只读不改不删；临时角色与临时用户结束时硬删。
运行：PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_role_api.py
"""

import asyncio
import logging
import secrets
import time

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from app.api.v1 import api_router
from app.core.config import settings
from app.db.session import async_session_factory, engine
from smoke_auth import SEED_ADMIN_ID, SEED_REVIEWER_ID, SEED_USER_ID, bearer_headers

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
)
# engine 以 echo=settings.DEBUG 创建，这里关掉避免 SQL 刷屏淹没断言输出
engine.echo = False

# 裸 app：不挂 lifespan，不连 MQ / ES / Neo4j
app = FastAPI()
app.include_router(api_router, prefix=settings.API_V1_PREFIX)

API = settings.API_V1_PREFIX

RUN_TAG = f"{int(time.time())}{secrets.token_hex(3)}"
ROLE_NAME = f"冒烟角色{RUN_TAG}"
ROLE_CODE = f"ROLE_SMOKE_{RUN_TAG}"
USER_NAME = f"smoke-role-{RUN_TAG}"
# 权限端点专用临时角色（避免污染上面那个会被删除的临时角色）
ROLE_PERM_NAME = f"冒烟权限角色{RUN_TAG}"
ROLE_PERM_CODE = f"ROLE_PERM_{RUN_TAG}"

# 种子角色 ID（init.sql 固定，只读断言用）
SEED_ROLE_IDS = {
    "2000000000000000001",
    "2000000000000000002",
    "2000000000000000003",
}


def show(step: str, resp: httpx.Response) -> None:
    print(f"  {step}: {resp.status_code} {resp.text[:120]}")


async def cleanup() -> None:
    """硬删临时角色与临时用户（按 role_code/username 前缀定位，先子表后主表）

    ⚠️ 子表必须全部先清：kh_role_permission（权限关联，FK 无 CASCADE）、
    kh_user_role（用户-角色关联，FK 无 CASCADE）、kh_user，再删 kh_role 主表。
    """
    async with async_session_factory() as session:
        # 权限关联（ROLE_PERM 角色在测试中会被赋权，崩溃残留时必有此表引用）
        await session.execute(
            text("DELETE FROM kh_role_permission WHERE role_id IN "
                 "(SELECT id FROM kh_role WHERE role_code = :rc)"), {"rc": ROLE_PERM_CODE},
        )
        await session.execute(
            text("DELETE FROM kh_user_role WHERE role_id IN "
                 "(SELECT id FROM kh_role WHERE role_code = :rc)"), {"rc": ROLE_CODE},
        )
        await session.execute(
            text("DELETE FROM kh_role WHERE role_code = :rc"), {"rc": ROLE_CODE}
        )
        await session.execute(
            text("DELETE FROM kh_user_role WHERE role_id IN "
                 "(SELECT id FROM kh_role WHERE role_code = :rc)"), {"rc": ROLE_PERM_CODE},
        )
        await session.execute(
            text("DELETE FROM kh_role WHERE role_code = :rc"), {"rc": ROLE_PERM_CODE}
        )
        await session.execute(
            text("DELETE FROM kh_user_role WHERE user_id IN "
                 "(SELECT id FROM kh_user WHERE username = :u)"), {"u": USER_NAME},
        )
        await session.execute(
            text("DELETE FROM kh_user WHERE username = :u"), {"u": USER_NAME}
        )
        await session.commit()
        print(f"🧹 已清理临时角色 {ROLE_CODE} 与临时用户 {USER_NAME}")


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://smoke"
    ) as client:
        admin_h = bearer_headers(SEED_ADMIN_ID)
        reviewer_h = bearer_headers(SEED_REVIEWER_ID)
        user_h = bearer_headers(SEED_USER_ID)

        # 1) 无 token → 401
        resp = await client.get(f"{API}/roles/list")
        show("1 list 无 token", resp)
        assert resp.status_code == 401, resp.text

        # 2) 仅 ROLE_USER 的 user → 403（类级 @Roles(ADMIN)）
        resp = await client.get(f"{API}/roles/list", headers=user_h)
        show("2 list user 角色", resp)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == "权限不足", resp.text

        # 3) REVIEWER（无 ADMIN）→ 403
        resp = await client.get(f"{API}/roles/list", headers=reviewer_h)
        show("3 list reviewer 角色", resp)
        assert resp.status_code == 403, resp.text

        # 4) admin list → 200：3 个种子角色按 roleName 升序，返回不含 status 键
        resp = await client.get(f"{API}/roles/list", headers=admin_h)
        show("4 list admin", resp)
        assert resp.status_code == 200, resp.text
        items = resp.json()
        assert {it["id"] for it in items} >= SEED_ROLE_IDS, f"种子角色缺失：{items}"
        assert items == sorted(items, key=lambda it: it["roleName"]), "未按 roleName 升序"
        assert all("status" not in it for it in items), "list 不应返回 status 键"

        # 5) admin get 种子角色详情 → 200 含 status=1
        resp = await client.get(
            f"{API}/roles/2000000000000000001", headers=admin_h
        )
        show("5 get 种子角色", resp)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == 1, resp.text

        # 6) get 不存在 / 非法 id → 404「角色不存在」
        resp = await client.get(f"{API}/roles/999999999999999999", headers=admin_h)
        show("6a get 不存在", resp)
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "角色不存在", resp.text
        resp = await client.get(f"{API}/roles/not-a-number", headers=admin_h)
        show("6b get 非法 id", resp)
        assert resp.status_code == 404, resp.text

        # 7) create → 201，返回不含 status 键（对齐 TS 内联对象）
        resp = await client.post(
            f"{API}/roles",
            headers=admin_h,
            json={"roleName": ROLE_NAME, "roleCode": ROLE_CODE, "description": "冒烟临时角色"},
        )
        show("7 create", resp)
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert "status" not in created, f"create 不应返回 status 键：{created}"
        role_id = created["id"]

        # 8) 同 roleCode 再建 → 409「角色编码已存在」
        resp = await client.post(
            f"{API}/roles",
            headers=admin_h,
            json={"roleName": "重复编码", "roleCode": ROLE_CODE},
        )
        show("8 create 重复 roleCode", resp)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "角色编码已存在", resp.text

        # 9) update 部分更新 → 200 含 status；禁用（status=0）
        resp = await client.put(
            f"{API}/roles/{role_id}",
            headers=admin_h,
            json={"description": "更新后的描述", "status": 0},
        )
        show("9 update 禁用", resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == 0 and body["description"] == "更新后的描述", resp.text
        assert body["roleName"] == ROLE_NAME, "未传的 roleName 不应被改动"

        # 10) 禁用后 list 不再包含该角色
        resp = await client.get(f"{API}/roles/list", headers=admin_h)
        assert all(it["id"] != role_id for it in resp.json()), "禁用角色不应出现在 list"
        show("10 list 不含禁用角色", resp)

        # 11) 恢复启用 + 建临时用户并授予该角色 → delete 拦截 400
        resp = await client.put(
            f"{API}/roles/{role_id}", headers=admin_h, json={"status": 1}
        )
        assert resp.status_code == 200 and resp.json()["status"] == 1, resp.text

        from app.common.snowflake import next_snowflake_id
        from app.models.user import USER_STATUS_ENABLED, User
        from app.services.user import user_service

        async with async_session_factory() as session:
            user_id = next_snowflake_id()
            session.add(
                User(
                    id=user_id,
                    username=USER_NAME,
                    password="not-a-real-login",
                    status=USER_STATUS_ENABLED,
                )
            )
            await session.commit()
            await user_service.assign_role(session, str(user_id), ROLE_CODE)

        resp = await client.delete(f"{API}/roles/{role_id}", headers=admin_h)
        show("11 delete 有关联用户", resp)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "角色仍有关联用户，无法删除", resp.text

        # 12) 解绑后 delete → 200「删除成功」；再查 → 404
        async with async_session_factory() as session:
            await session.execute(
                text("DELETE FROM kh_user_role WHERE role_id = :r"), {"r": int(role_id)}
            )
            await session.commit()
        resp = await client.delete(f"{API}/roles/{role_id}", headers=admin_h)
        show("12 delete 解绑后", resp)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"message": "删除成功"}, resp.text
        resp = await client.get(f"{API}/roles/{role_id}", headers=admin_h)
        assert resp.status_code == 404, "删除后应 404"
        show("12b 删除后再查 404", resp)

        # 13) reviewer（无 system:role 权限）访问 /permissions → 403（细粒度守卫）
        resp = await client.get(
            f"{API}/roles/2000000000000000003/permissions", headers=reviewer_h
        )
        show("13 reviewer 细粒度守卫", resp)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == "权限不足", resp.text

        # 14) admin 读种子角色(user)的权限 → 200，且含 init.sql 写入的 6 个权限 ID
        resp = await client.get(
            f"{API}/roles/2000000000000000003/permissions", headers=admin_h
        )
        show("14 admin 读种子角色权限", resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["roleId"] == "2000000000000000003", resp.text
        assert set(body["permissionIds"]) == {
            "4000000000000000001",
            "4000000000000000002",
            "4000000000000000011",
            "4000000000000000012",
            "4000000000000000003",
            "4000000000000000004",
        }, resp.text

        # 15) 建一个临时角色用于权限写测试
        resp = await client.post(
            f"{API}/roles",
            headers=admin_h,
            json={"roleName": ROLE_PERM_NAME, "roleCode": ROLE_PERM_CODE},
        )
        show("15 create 权限测试角色", resp)
        assert resp.status_code == 201, resp.text
        perm_role_id = resp.json()["id"]

        # 16) 新角色初始无权限 → GET 返回空数组（roleId 透传）
        resp = await client.get(
            f"{API}/roles/{perm_role_id}/permissions", headers=admin_h
        )
        show("16 新角色权限为空", resp)
        assert resp.status_code == 200 and resp.json()["permissionIds"] == [], resp.text

        # 17) PUT 授予两个有效权限 ID（document:list / document:create）→ 200 回显
        valid_ids = ["4000000000000000011", "4000000000000000012"]
        resp = await client.put(
            f"{API}/roles/{perm_role_id}/permissions",
            headers=admin_h,
            json={"permissionIds": valid_ids},
        )
        show("17 PUT 授予权限", resp)
        assert resp.status_code == 200, resp.text
        assert set(resp.json()["permissionIds"]) == set(valid_ids), resp.text

        # 18) 再 GET 确认已落库
        resp = await client.get(
            f"{API}/roles/{perm_role_id}/permissions", headers=admin_h
        )
        assert set(resp.json()["permissionIds"]) == set(valid_ids), resp.text
        show("18 确认授予生效", resp)

        # 19) PUT 空数组 → 清空（全量替换语义）
        resp = await client.put(
            f"{API}/roles/{perm_role_id}/permissions",
            headers=admin_h,
            json={"permissionIds": []},
        )
        show("19 PUT 清空权限", resp)
        assert resp.status_code == 200 and resp.json()["permissionIds"] == [], resp.text

        # 20) 不存在的角色 GET 权限 → 404「角色不存在」
        resp = await client.get(
            f"{API}/roles/999999999999999999/permissions", headers=admin_h
        )
        show("20 假角色 GET 权限 404", resp)
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "角色不存在", resp.text

        # 收尾：删掉权限测试角色
        await client.delete(f"{API}/roles/{perm_role_id}", headers=admin_h)

        print("✅ 全部断言通过（20 组）")


if __name__ == "__main__":
    # main + cleanup 同一事件循环：engine 连接池绑定首个 loop，
    # 跨 loop 调 cleanup 会报 "attached to a different loop"，故用 try/finally 而非
    # except 里再 asyncio.run(cleanup())。
    async def _run() -> None:
        try:
            await main()
        finally:
            await cleanup()

    asyncio.run(_run())
