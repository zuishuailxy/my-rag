"""users 模块管理端 E2E 冒烟（真实 PG，裸 FastAPI app + ASGITransport）

覆盖 UserController 全部端点：
  401 未登录 → 403 普通用户 → me/stats → PUT me → 改密（旧错 400 / 成功 /
  旧密 401 / 新密登录）→ 分页筛选（keyword/status/roleCode）→ 详情 VO →
  角色码 → 替换角色（404 / 空数组 422 / 生效验证）→ 管理员部分更新 →
  重置密码 → 软删除（404 / 分页消失 / 登录 401）→ 创建用户（403 / 默认
  ROLE_USER / 409 查重 / 空数组回退 / 未知角色 404 且用户留存（TS 非事务
  边界 quirk）/ 全字段 + 禁用 / 新用户可登录）→ 用户权限接口（GET/PUT
  /users/{id}/permissions：空权限 / 403 普通用户 / 401 匿名 / 授权生效 /
  空数组清空 / 非法 id 404 / 普通用户 403）。

种子账号只读（用 make_access_token 免登录写库）；临时用户直插 DB（绕开
register 的邮箱验证分叉，.env 的 REQUIRE_EMAIL_VERIFICATION=true 不影响），
API 创建的用户由 cleanup 按 username 清单硬删（先子表后主表）。

运行：cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_user_api.py
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx
from sqlalchemy import text

from app.common.snowflake import next_snowflake_id
from app.db.session import async_session_factory, engine
from app.main import app
from app.models.user import User
from app.schemas.auth import BCRYPT_MAX_PASSWORD_BYTES
from app.services.user import _hash_password
from scripts.smoke_auth import SEED_USER_ID, SEED_USER_USERNAME, bearer_headers

# 兜底：DB_ECHO 打开时手动压掉 SQL，避免刷屏淹没断言输出（默认已由 DB_ECHO=false 关闭）
engine.echo = False

BASE = "http://testserver/api/v1"
PASSWORD = "temp-pass-6"
RUN_TAG = f"{int(datetime.now(timezone.utc).timestamp() * 1000) % 10_000_000_000}"
USERNAME = f"smoke-user-{RUN_TAG}"
EMAIL = f"smoke-user-{RUN_TAG}@example.com"

# API 创建用户步骤产生的 username（cleanup 逐一硬删；失败中断时也兜底清掉）
CREATED_USERNAMES: list[str] = []

_results: list[str] = []


def check(step: int | float, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    _results.append(f"{mark} {step:>4g} {detail}")
    if not ok:
        raise AssertionError(f"第 {step} 步失败：{detail}")


async def create_temp_user() -> tuple[str, str]:
    """直插临时用户（status=1 / email_verified=1 + USER 角色），返回 (id, username)"""
    user_id = next_snowflake_id()
    async with async_session_factory() as session:
        session.add(
            User(
                id=user_id,
                username=USERNAME,
                password=_hash_password(PASSWORD),
                email=EMAIL,
                real_name="冒烟临时用户",
                status=1,
                email_verified=1,
            )
        )
        await session.commit()
        # 授 USER 角色（种子角色 2000000000000000003，见 init.sql 种子段）
        await session.execute(
            text(
                "INSERT INTO kh_user_role (id, user_id, role_id) "
                "VALUES (:id, :uid, 2000000000000000003)"
            ),
            {"id": next_snowflake_id(), "uid": user_id},
        )
        await session.commit()
    return str(user_id), USERNAME


async def cleanup() -> None:
    """硬删临时用户（先子表后主表，kh_user_role 两个 FK 无 CASCADE）

    覆盖两类：直插的 USERNAME 与 API 创建的 CREATED_USERNAMES。
    """
    usernames = [USERNAME, *CREATED_USERNAMES]
    async with async_session_factory() as session:
        for name in usernames:
            row = await session.execute(
                text("SELECT id FROM kh_user WHERE username = :u"), {"u": name}
            )
            user_id = row.scalar()
            if user_id is None:
                continue
            await session.execute(
                text("DELETE FROM kh_user_permission WHERE user_id = :i"), {"i": user_id}
            )
            await session.execute(
                text("DELETE FROM kh_user_role WHERE user_id = :i"), {"i": user_id}
            )
            await session.execute(
                text("DELETE FROM kh_user WHERE id = :i"), {"i": user_id}
            )
            print(f"🧹 已删除临时用户 {name}（id={user_id}）")
        await session.commit()


async def login(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    return await client.post(
        f"{BASE}/auth/login", json={"username": username, "password": password}
    )


async def main() -> None:
    user_id, username = await create_temp_user()
    admin_headers = bearer_headers()  # 种子 admin（ADMIN+REVIEWER）
    user_headers = bearer_headers(SEED_USER_ID, SEED_USER_USERNAME)  # 仅 USER 角色
    temp_headers = bearer_headers(user_id, username)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        # 1. 未登录 401（守卫默认全拦）
        r = await client.get("/users/page")
        check(1, r.status_code == 401, f"未登录 GET /users/page → {r.status_code}")

        # 2. 普通用户 403（403 早于业务 404/400）
        r = await client.get("/users/page", headers=user_headers)
        check(2, r.status_code == 403, f"USER 角色 GET /users/page → {r.status_code}")

        # 3. me/stats：新用户全 0
        r = await client.get("/users/me/stats", headers=temp_headers)
        body = r.json()
        check(
            3,
            r.status_code == 200
            and body["documentCount"] == 0
            and body["viewCount"] == 0
            and body["likeCount"] == 0
            and body["commentCount"] == 0,
            f"GET /users/me/stats → {body}",
        )

        # 4. PUT /me 部分更新资料
        r = await client.put(
            "/users/me",
            headers=temp_headers,
            json={"realName": "冒烟改名", "avatar": "https://example.com/a.png"},
        )
        body = r.json()
        check(
            4,
            r.status_code == 200
            and body["realName"] == "冒烟改名"
            and body["username"] == username
            and body["email"] == EMAIL,
            f"PUT /users/me → {r.status_code} realName={body.get('realName')}",
        )

        # 5. 改密：原密码错误 400
        r = await client.put(
            "/users/password/change",
            headers=temp_headers,
            json={"oldPassword": "wrong-pass", "newPassword": "new-pass-6"},
        )
        check(5, r.status_code == 400, f"原密码错误 → {r.status_code} {r.json()}")

        # 6. 改密成功 → 旧密码 401 → 新密码登录 200
        r = await client.put(
            "/users/password/change",
            headers=temp_headers,
            json={"oldPassword": PASSWORD, "newPassword": "new-pass-6"},
        )
        check(6, r.status_code == 200 and r.json()["message"] == "密码修改成功", f"改密 → {r.status_code}")
        r = await login(client, username, PASSWORD)
        check(6.1, r.status_code == 401, f"旧密码登录 → {r.status_code}")
        r = await login(client, username, "new-pass-6")
        check(6.2, r.status_code == 201, f"新密码登录 → {r.status_code}（@Post 默认 201）")

        # 7. 分页 keyword 筛选命中临时用户；roleCode=ROLE_USER 命中
        r = await client.get(
            "/users/page", headers=admin_headers, params={"keyword": username}
        )
        body = r.json()
        check(
            7,
            r.status_code == 200
            and body["total"] == 1
            and body["items"][0]["id"] == user_id
            and body["page"] == 1
            and body["pageSize"] == 20,
            f"分页 keyword → total={body.get('total')}",
        )
        r = await client.get(
            "/users/page", headers=admin_headers, params={"roleCode": "ROLE_USER", "pageSize": 100}
        )
        ids = [it["id"] for it in r.json()["items"]]
        check(7.1, user_id in ids, f"分页 roleCode=ROLE_USER 含临时用户（{len(ids)} 人）")
        r = await client.get(
            "/users/page", headers=admin_headers, params={"status": 1, "pageSize": 100}
        )
        check(
            7.2,
            r.status_code == 200 and all(it["status"] == 1 for it in r.json()["items"]),
            "分页 status=1 全部启用",
        )

        # 8. 详情 VO（含 roleCodes）
        r = await client.get(f"/users/{user_id}", headers=admin_headers)
        body = r.json()
        check(
            8,
            r.status_code == 200
            and body["roleCodes"] == ["ROLE_USER"]
            and "password" not in body
            and body["lastLoginAt"] is not None,  # 第 6.2 步登录写过 last_login_at
            f"GET /users/{{id}} → roleCodes={body.get('roleCodes')}",
        )

        # 9. 角色码接口
        r = await client.get(f"/users/{user_id}/roles", headers=admin_headers)
        check(
            9,
            r.status_code == 200
            and r.json() == {"userId": user_id, "roleCodes": ["ROLE_USER"]},
            f"GET /users/{{id}}/roles → {r.json()}",
        )

        # 10. 替换角色为 REVIEWER → 旧角色消失、新角色生效
        r = await client.put(
            f"/users/{user_id}/roles",
            headers=admin_headers,
            json={"roleCodes": ["ROLE_REVIEWER"]},
        )
        check(
            10,
            r.status_code == 200
            and r.json()["roleCodes"] == ["ROLE_REVIEWER"],
            f"PUT roles → {r.json().get('roleCodes')}",
        )
        r = await client.get(
            "/users/page", headers=admin_headers, params={"roleCode": "ROLE_REVIEWER", "pageSize": 100}
        )
        check(10.1, user_id in [it["id"] for it in r.json()["items"]], "roleCode=ROLE_REVIEWER 命中")
        r = await client.get(
            "/users/page", headers=admin_headers, params={"roleCode": "ROLE_USER", "pageSize": 100}
        )
        check(10.2, user_id not in [it["id"] for it in r.json()["items"]], "roleCode=ROLE_USER 不再命中")

        # 11. 未知角色 404（保真 TS 文案：冒号 + 逗号分隔）
        r = await client.put(
            f"/users/{user_id}/roles",
            headers=admin_headers,
            json={"roleCodes": ["ROLE_REVIEWER", "ROLE_NOPE"]},
        )
        check(11, r.status_code == 404 and "角色不存在" in r.json()["detail"], f"未知角色 → {r.status_code}")

        # 12. 空数组 422（TS ArrayNotEmpty → 400，FastAPI 刻意 422，见 schemas/user.py）
        r = await client.put(
            f"/users/{user_id}/roles", headers=admin_headers, json={"roleCodes": []}
        )
        check(12, r.status_code == 422, f"空数组 → {r.status_code}")

        # 13. 管理员部分更新：只传 status，其余字段不动
        r = await client.put(
            f"/users/{user_id}", headers=admin_headers, json={"status": 0}
        )
        body = r.json()
        check(
            13,
            r.status_code == 200
            and body["status"] == 0
            and body["realName"] == "冒烟改名",  # 未传字段不改动
            f"PUT /users/{{id}} status=0 → realName={body.get('realName')}",
        )

        # 14. 管理员重置密码 → 新密码可登录
        r = await client.put(
            f"/users/{user_id}/password/reset",
            headers=admin_headers,
            json={"newPassword": "reset-pass-6"},
        )
        check(14, r.status_code == 200 and r.json()["message"] == "密码重置成功", f"重置密码 → {r.status_code}")
        r = await login(client, username, "reset-pass-6")
        check(14.1, r.status_code == 401, f"禁用账户登录 → {r.status_code}（status=0 拦截）")

        # 15. 软删除 → 详情 404 / 分页消失 / 登录 401
        r = await client.delete(f"/users/{user_id}", headers=admin_headers)
        check(15, r.status_code == 200 and r.json()["message"] == "删除成功", f"DELETE → {r.status_code}")
        r = await client.get(f"/users/{user_id}", headers=admin_headers)
        check(15.1, r.status_code == 404, f"删除后详情 → {r.status_code}")
        r = await client.get(
            "/users/page", headers=admin_headers, params={"keyword": username}
        )
        check(15.2, r.json()["total"] == 0, "删除后分页不出现（deleted=false 过滤）")
        r = await login(client, username, "reset-pass-6")
        check(15.3, r.status_code == 401, f"软删用户登录 → {r.status_code}")

        # 16. 普通用户访问他人详情 403
        r = await client.get("/users/1000000000000000001", headers=user_headers)
        check(16, r.status_code == 403, f"USER 角色 GET /users/{{id}} → {r.status_code}")

        # ------------------------------------------------ POST /users 创建用户
        created_password = "created-pass-6"
        new_username = f"smoke-new-{RUN_TAG}"
        empty_role_username = f"smoke-empty-{RUN_TAG}"
        orphan_username = f"smoke-orphan-{RUN_TAG}"
        full_username = f"smoke-full-{RUN_TAG}"
        CREATED_USERNAMES.extend(
            [new_username, empty_role_username, orphan_username, full_username]
        )

        # 17. 普通用户创建用户 403
        r = await client.post(
            "/users", headers=user_headers, json={"username": new_username, "password": created_password}
        )
        check(17, r.status_code == 403, f"USER 角色 POST /users → {r.status_code}")

        # 18. 最小字段创建：默认 status=1 + 默认 ROLE_USER，emailVerified 恒 1 不在 VO
        r = await client.post(
            "/users",
            headers=admin_headers,
            json={"username": new_username, "password": created_password},
        )
        body = r.json()
        new_user_id = body["id"]  # 供下方权限接口用例复用（未被删除，仅 ROLE_USER）
        check(
            18,
            r.status_code == 201
            and body["username"] == new_username
            and body["status"] == 1
            and body["roleCodes"] == ["ROLE_USER"]
            and body["email"] is None
            and body["realName"] is None
            and "password" not in body
            and "emailVerified" not in body,  # UserVO 本就不含该字段
            f"POST /users 最小字段 → {r.status_code} roleCodes={body.get('roleCodes')}",
        )

        # 18.1 用户名重复 409（createUser 只查 username，不查 email——TS 同款）
        r = await client.post(
            "/users",
            headers=admin_headers,
            json={"username": new_username, "password": created_password},
        )
        check(
            18.1,
            r.status_code == 409 and r.json()["detail"] == "用户名已存在",
            f"重复用户名 → {r.status_code} {r.json()}",
        )

        # 18.2 空数组回退默认 ROLE_USER（TS `?.length ? : ` 语义）
        r = await client.post(
            "/users",
            headers=admin_headers,
            json={"username": empty_role_username, "password": created_password, "roleCodes": []},
        )
        check(
            18.2,
            r.status_code == 201 and r.json()["roleCodes"] == ["ROLE_USER"],
            f"roleCodes=[] → {r.status_code} roleCodes={r.json().get('roleCodes')}",
        )

        # 18.3 未知角色 404，且用户已留存（TS 非事务边界：先 save 后 replaceRoles，
        # replaceRoles 抛异常用户不回滚）——锁定该 quirk
        r = await client.post(
            "/users",
            headers=admin_headers,
            json={"username": orphan_username, "password": created_password, "roleCodes": ["ROLE_NOPE"]},
        )
        check(18.3, r.status_code == 404 and "角色不存在" in r.json()["detail"], f"未知角色 → {r.status_code}")
        r = await client.get(
            "/users/page", headers=admin_headers, params={"keyword": orphan_username}
        )
        orphan = r.json()["items"]
        check(
            18.4,
            r.status_code == 200
            and len(orphan) == 1
            and orphan[0]["roleCodes"] == [],
            "角色校验失败的用户仍留存（无角色，TS 两次独立 save 同款）",
        )

        # 18.5 全字段创建：status=0 / 指定角色 / email+realName+avatar 均落库
        full_email = f"smoke-full-{RUN_TAG}@example.com"
        r = await client.post(
            "/users",
            headers=admin_headers,
            json={
                "username": full_username,
                "password": created_password,
                "email": full_email,
                "realName": "全字段用户",
                "avatar": "https://example.com/b.png",
                "status": 0,
                "roleCodes": ["ROLE_REVIEWER"],
            },
        )
        body = r.json()
        check(
            18.5,
            r.status_code == 201
            and body["status"] == 0
            and body["roleCodes"] == ["ROLE_REVIEWER"]
            and body["email"] == full_email
            and body["realName"] == "全字段用户",
            f"POST /users 全字段 → status={body.get('status')} roleCodes={body.get('roleCodes')}",
        )

        # 18.6 新用户可登录（email_verified=1 恒生效）；禁用的新用户 401
        r = await login(client, new_username, created_password)
        check(18.6, r.status_code == 201, f"新建用户登录 → {r.status_code}（status=1 直接可登）")
        r = await login(client, full_username, created_password)
        check(18.7, r.status_code == 401, f"禁用新建用户登录 → {r.status_code}")

        # -------------------------------------- GET/PUT /users/{id}/permissions 权限接口
        # new_user_id 仅 ROLE_USER：ROLE_USER 在种子里绑定了若干权限，故 permissionCodes
        # 非空（角色派生），directPermissionIds 为空（无直接授权）——与合并口径一致
        r = await client.get(f"/users/{new_user_id}/permissions", headers=admin_headers)
        body_p = r.json()
        check(
            19,
            r.status_code == 200
            and body_p["userId"] == new_user_id
            and isinstance(body_p["permissionCodes"], list)
            and len(body_p["permissionCodes"]) > 0
            and body_p["directPermissionIds"] == [],
            f"GET 权限（USER 用户）→ {body_p}",
        )
        # 19.1 普通用户 403（system:user 细粒度守卫，403 早于业务）
        r = await client.get(f"/users/{new_user_id}/permissions", headers=user_headers)
        check(19.1, r.status_code == 403, f"USER 角色 GET 权限 → {r.status_code}")
        # 19.2 匿名 401
        r = await client.get(f"/users/{new_user_id}/permissions")
        check(19.2, r.status_code == 401, f"未登录 GET 权限 → {r.status_code}")

        # 取一个种子权限（status=1）用于授权用例，避免污染种子数据
        r = await client.get("/permissions/list", headers=admin_headers)
        perm = r.json()[0]
        perm_id = perm["id"]
        perm_code = perm["permissionCode"]

        # 20. 全量替换直接授权：PUT → 200 + permissionIds
        r = await client.put(
            f"/users/{new_user_id}/permissions",
            headers=admin_headers,
            json={"permissionIds": [perm_id]},
        )
        body_p = r.json()
        check(
            20,
            r.status_code == 200
            and body_p["userId"] == new_user_id
            and body_p["permissionIds"] == [perm_id],
            f"PUT 权限 → {body_p}",
        )
        # 20.1 授权后 GET 反映 directPermissionIds + permissionCodes 含该码（直接授权 status=1）
        r = await client.get(f"/users/{new_user_id}/permissions", headers=admin_headers)
        body_p = r.json()
        check(
            20.1,
            body_p["directPermissionIds"] == [perm_id] and perm_code in body_p["permissionCodes"],
            f"授权后 GET → direct={body_p['directPermissionIds']} codes={body_p['permissionCodes']}",
        )
        # 20.2 空数组清空授权
        r = await client.put(
            f"/users/{new_user_id}/permissions", headers=admin_headers, json={"permissionIds": []}
        )
        check(20.2, r.status_code == 200 and r.json()["permissionIds"] == [], f"空数组清空 → {r.json()}")
        r = await client.get(f"/users/{new_user_id}/permissions", headers=admin_headers)
        check(20.3, r.json()["directPermissionIds"] == [], "清空后 directPermissionIds 为空")
        # 20.4 合法但不存在的权限 id → 404（超界 id 会被 parse_bigint 判非法 → 400，另属分支）
        r = await client.put(
            f"/users/{new_user_id}/permissions",
            headers=admin_headers,
            json={"permissionIds": ["1234567890123456789"]},
        )
        check(20.4, r.status_code == 404, f"不存在的权限 id → {r.status_code}")
        # 20.5 普通用户授权 → 403
        r = await client.put(
            f"/users/{new_user_id}/permissions",
            headers=user_headers,
            json={"permissionIds": [perm_id]},
        )
        check(20.5, r.status_code == 403, f"USER 角色 PUT 权限 → {r.status_code}")


async def run_all() -> None:
    """main + cleanup 必须同事件循环（engine 连接池绑定首个 loop）"""
    try:
        await main()
    finally:
        await cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run_all())
    print("\n".join(_results))
    print(f"\n共 {len(_results)} 步断言全部通过")
