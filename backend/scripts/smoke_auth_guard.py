"""
鉴权守卫冒烟测试（route_class 全局守卫 + 真实 Postgres）

背景：FastAPI 没有 NestJS 的 APP_GUARD，改用自定义 APIRoute（app/api/auth_route.py）
等价实现 JwtAuthGuard + JwtStrategy + RolesGuard。本脚本验证守卫的实际拦截行为，
不依赖 lifespan，因此不连 MQ / ES / Neo4j，不消耗 LLM token。

覆盖：
  1  公开接口（health / login）无需 token
  2  受保护接口无 token → 401「未登录或 token 已失效」+ WWW-Authenticate: Bearer
  3  签名被篡改 → 401 同文案（过期/篡改/格式非法统一，不泄露原因）
  4  refresh token 当 access 用 → 401「无效的 access token」
  5  有效 access token → documents 列表 200
  6  @CurrentUser 等价物：/auth/me 返回守卫解析出的用户（roles 来自 kh_user_role）
  7  角色守卫：ROLE_ADMIN / ROLE_REVIEWER → 200；ROLE_USER → 403「权限不足」
  8  禁用账户持有效 token → 401「账户已禁用」
  9  已软删用户持有效 token → 404「用户不存在」
     （对应 TS handleRequest 的 `throw err`：Strategy 抛的错原样透出，不是 401）
 10  OpenAPI：受保护路由带 security、公开路由不带

依赖 init.sql 种子账号：admin（ADMIN+REVIEWER）/ reviewer（REVIEWER）/ user（USER），
密码均为 123456。步骤 8、9 会临时改 kh_user，finally 中原样恢复。

运行：PYTHONPATH=. .venv/bin/python scripts/smoke_auth_guard.py
"""

import asyncio
import logging

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from app.api.v1 import api_router
from app.core.config import settings
from app.db.session import async_session_factory, engine

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
)
# 兜底：DB_ECHO 打开时手动压掉 SQL，避免刷屏淹没断言输出（默认已由 DB_ECHO=false 关闭）
engine.echo = False

# 裸 app：不挂 lifespan，不连 MQ / ES / Neo4j
app = FastAPI()
app.include_router(api_router, prefix=settings.API_V1_PREFIX)

API = settings.API_V1_PREFIX
PASSWORD = "123456"

UNAUTHORIZED = "未登录或 token 已失效"
INVALID_ACCESS = "无效的 access token"
FORBIDDEN = "权限不足"
DISABLED = "账户已禁用"
NOT_FOUND = "用户不存在"


def show(step: str, resp: httpx.Response) -> None:
    body = resp.text
    if len(body) > 160:
        body = body[:160] + "..."
    print(f"{step} -> {resp.status_code} {body}")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def login(client: httpx.AsyncClient, username: str) -> dict:
    """登录并返回完整 LoginResult（含 accessToken / refreshToken）"""
    resp = await client.post(
        f"{API}/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 201, f"{username} 登录失败：{resp.text}"
    return resp.json()


async def restore_seed_users() -> None:
    """把测试期间改过的 kh_user 恢复原状

    除 status / deleted 外还要清 last_login_at —— 登录会经 touch_last_login 写入它，
    不清会让依赖「种子账号从未登录」的脚本（smoke_rbac_models）失败。
    """
    async with async_session_factory() as session:
        await session.execute(
            text(
                "UPDATE kh_user "
                "SET status = 1, deleted = false, last_login_at = NULL "
                "WHERE username IN ('admin', 'reviewer', 'user')"
            )
        )
        await session.commit()


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://smoke"
    ) as client:
        # 1) 公开接口无需 token
        resp = await client.get(f"{API}/health")
        show("1 health（公开，无 token）", resp)
        assert resp.status_code == 200, resp.text

        # 2) 三个种子账号登录
        admin = await login(client, "admin")
        reviewer = await login(client, "reviewer")
        plain_user = await login(client, "user")
        print(
            "2 登录成功：admin roles=%s | reviewer roles=%s | user roles=%s"
            % (
                admin["userInfo"]["roles"],
                reviewer["userInfo"]["roles"],
                plain_user["userInfo"]["roles"],
            )
        )
        assert set(admin["userInfo"]["roles"]) == {"ROLE_ADMIN", "ROLE_REVIEWER"}
        assert reviewer["userInfo"]["roles"] == ["ROLE_REVIEWER"]
        assert plain_user["userInfo"]["roles"] == ["ROLE_USER"]
        # permissions：管理员追加 ADMIN_OPERATION_PERMISSIONS（含 system:user 等）；
        # 普通用户为 ROLE_USER 绑定的角色权限码（非空、不含管理码）
        assert "system:user" in admin["userInfo"]["permissions"], admin["userInfo"]
        assert len(plain_user["userInfo"]["permissions"]) > 0
        assert "system:user" not in plain_user["userInfo"]["permissions"]
        admin_at = admin["accessToken"]
        admin_rt = admin["refreshToken"]

        # 3) 受保护接口无 token → 401，且带 Bearer 挑战头
        resp = await client.get(f"{API}/documents")
        show("3 GET /documents（无 token）", resp)
        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"] == UNAUTHORIZED, resp.text
        assert resp.headers.get("www-authenticate") == "Bearer"

        # 4) 签名被篡改 → 401 同文案（不区分过期 / 篡改 / 格式非法）
        resp = await client.get(f"{API}/documents", headers=bearer(admin_at + "x"))
        show("4 GET /documents（篡改签名）", resp)
        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"] == UNAUTHORIZED, resp.text

        # 5) refresh token 不能当 access token 用
        resp = await client.get(f"{API}/documents", headers=bearer(admin_rt))
        show("5 GET /documents（用 refresh token）", resp)
        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"] == INVALID_ACCESS, resp.text

        # 6) 有效 access token
        resp = await client.get(f"{API}/documents", headers=bearer(admin_at))
        show("6 GET /documents（有效 token）", resp)
        assert resp.status_code == 200, resp.text

        # 7) @CurrentUser 等价物：/auth/me 复用守卫解析结果
        resp = await client.get(f"{API}/auth/me", headers=bearer(admin_at))
        show("7 GET /auth/me", resp)
        assert resp.status_code == 200, resp.text
        me = resp.json()
        assert me["username"] == "admin"
        assert me["userId"] == admin["userInfo"]["userId"]
        assert set(me["roles"]) == {"ROLE_ADMIN", "ROLE_REVIEWER"}
        # me 的 permissions 与登录响应一致（同一 build_auth_user 组装）
        assert set(me["permissions"]) == set(admin["userInfo"]["permissions"])

        # 8) 角色守卫：ADMIN / REVIEWER 放行，USER 拒绝
        resp = await client.get(
            f"{API}/auth/reviewer-ids", headers=bearer(admin_at)
        )
        show("8 reviewer-ids（admin，ADMIN+REVIEWER）", resp)
        assert resp.status_code == 200, resp.text

        resp = await client.get(
            f"{API}/auth/reviewer-ids", headers=bearer(reviewer["accessToken"])
        )
        show("9 reviewer-ids（reviewer，REVIEWER）", resp)
        assert resp.status_code == 200, resp.text

        resp = await client.get(
            f"{API}/auth/reviewer-ids", headers=bearer(plain_user["accessToken"])
        )
        show("10 reviewer-ids（user，仅 ROLE_USER）", resp)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == FORBIDDEN, resp.text

        # 9) 禁用账户：token 仍然有效，但查库后按禁用处理
        async with async_session_factory() as session:
            await session.execute(
                text("UPDATE kh_user SET status = 0 WHERE username = 'user'")
            )
            await session.commit()
        resp = await client.get(
            f"{API}/auth/me", headers=bearer(plain_user["accessToken"])
        )
        show("11 禁用账户持有效 token", resp)
        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"] == DISABLED, resp.text

        # 10) 已软删用户：Strategy 抛的 404 原样透出（TS handleRequest 的 throw err）
        async with async_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE kh_user SET status = 1, deleted = true "
                    "WHERE username = 'user'"
                )
            )
            await session.commit()
        resp = await client.get(
            f"{API}/auth/me", headers=bearer(plain_user["accessToken"])
        )
        show("12 已软删用户持有效 token", resp)
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == NOT_FOUND, resp.text

        # 11) OpenAPI：受保护路由带 security，公开路由不带
        schema = app.openapi()
        paths = schema["paths"]
        protected = paths[f"{API}/documents"]["get"].get("security")
        public_op = paths[f"{API}/health"]["get"].get("security")
        me_sec = paths[f"{API}/auth/me"]["get"].get("security")
        reviewer_sec = paths[f"{API}/auth/reviewer-ids"]["get"].get("security")
        print(
            f"13 OpenAPI：/documents security={protected} | /health security={public_op}"
            f" | /auth/me security={me_sec}"
        )
        assert protected == [{"HTTPBearer": []}]
        assert not public_op
        assert me_sec == [{"HTTPBearer": []}]
        assert reviewer_sec == [{"HTTPBearer": []}]

    try:
        await restore_seed_users()
        print("14 种子账号已恢复（status=1, deleted=false）")
    finally:
        from app.db.session import close_db_engine

        await close_db_engine()


if __name__ == "__main__":
    asyncio.run(main())
