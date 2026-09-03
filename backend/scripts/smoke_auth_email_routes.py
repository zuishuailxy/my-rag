"""auth 邮箱/密码路由 E2E 冒烟（对应 TS AuthController 的 4 个新端点）

覆盖路由（app/api/v1/auth.py）：
  - POST /auth/logout                    需登录（TS 未标 @Public），201
  - GET  /auth/verify-email?token=...    公开，200；令牌一次性消费
  - POST /auth/password/reset/send-code  公开，201；60s 冷却、未注册邮箱 404
  - POST /auth/password/reset            公开，201；成功后删码，新密码可登录

与 smoke_email_flow.py（服务层直测）互补，本脚本走完整 HTTP 链路：
裸 FastAPI app（不挂 lifespan）+ httpx.ASGITransport，真实 PG + Redis。
邮件发送猴子补丁（不真发 SMTP），因此 REQUIRE_EMAIL_VERIFICATION 开/关都能跑。

注册的是全新临时用户（不动种子账号）；结束时硬删临时用户 + 清 Redis 键。
运行：PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_auth_email_routes.py
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
from app.db.redis import redis_service
from app.db.session import async_session_factory, engine
from app.services.email import email_service
from smoke_auth import SEED_ADMIN_ID, bearer_headers

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
)
# engine 以 echo=settings.DEBUG 创建，这里关掉避免 SQL 刷屏淹没断言输出
engine.echo = False

# 裸 app：不挂 lifespan，不连 MQ / ES / Neo4j（Redis 手动 start，见 main）
app = FastAPI()
app.include_router(api_router, prefix=settings.API_V1_PREFIX)

API = settings.API_V1_PREFIX

# 邮件猴子补丁：捕获 _send 收到的 EmailMessage，不真发 SMTP
_sent_messages: list[object] = []


async def _fake_send(message: object) -> None:
    _sent_messages.append(message)


email_service._send = _fake_send  # type: ignore[method-assign]

RUN_TAG = f"{int(time.time())}{secrets.token_hex(3)}"
USERNAME = f"smoke-route-{RUN_TAG}"
# 邮箱用 example.com：email-validator 会拒收 .test / .invalid 等保留域（EmailStr 422）
EMAIL = f"smoke-route-{RUN_TAG}@example.com"
PASSWORD = "oldpass123"
NEW_PASSWORD = "newpass456"


def show(step: str, resp: httpx.Response) -> None:
    print(f"  {step}: {resp.status_code} {resp.text[:120]}")


def reset_code_key(email: str) -> str:
    return f"password:reset:code:{email.lower()}"


async def cleanup() -> None:
    """删除临时用户（先子表后主表，kh_user_role 两个 FK 无 CASCADE）+ 清 Redis 键"""
    async with async_session_factory() as session:
        row = await session.execute(
            text("SELECT id FROM kh_user WHERE username = :u"), {"u": USERNAME}
        )
        user_id = row.scalar()
        if user_id is not None:
            await session.execute(
                text("DELETE FROM kh_user_role WHERE user_id = :i"), {"i": user_id}
            )
            await session.execute(
                text("DELETE FROM kh_user WHERE id = :i"), {"i": user_id}
            )
            await session.commit()
            print(f"🧹 已删除临时用户 {USERNAME}（id={user_id}）")
    # 激活令牌两侧键（token→userId、userId→token）与重置验证码在正常流程中
    # 都会被消费/删除，这里兜底清一次——中途失败时 token 侧键不会留孤儿
    uid = str(user_id) if user_id is not None else ""
    user_side = await redis_service.get(f"email:activation:user:{uid}")
    keys = [reset_code_key(EMAIL), f"email:activation:user:{uid}"]
    if user_side:
        keys.append(f"email:activation:token:{user_side}")
    await redis_service.delete(*keys)


async def main() -> None:
    await redis_service.start()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://smoke"
        ) as client:
            # 1) logout 未带 token → 401（TS logout 没标 @Public，同样需登录）
            resp = await client.post(f"{API}/auth/logout")
            show("1 logout 无 token", resp)
            assert resp.status_code == 401, resp.text

            # 2) logout 带 token → 201「已退出登录」（无状态 JWT，服务端无会话可销毁）
            resp = await client.post(
                f"{API}/auth/logout", headers=bearer_headers(SEED_ADMIN_ID)
            )
            show("2 logout 带 token", resp)
            assert resp.status_code == 201, resp.text
            assert resp.json() == {"message": "已退出登录"}, resp.text

            # 3) verify-email 缺 token query → 422（刻意优于 TS：参数校验前置）
            resp = await client.get(f"{API}/auth/verify-email")
            show("3 verify-email 缺 token", resp)
            assert resp.status_code == 422, resp.text

            # 4) verify-email 坏 token → 400「激活链接无效或已过期」
            resp = await client.get(
                f"{API}/auth/verify-email", params={"token": "not-a-real-token"}
            )
            show("4 verify-email 坏 token", resp)
            assert resp.status_code == 400, resp.text
            assert resp.json()["detail"] == "激活链接无效或已过期", resp.text

            # 5) 注册临时用户（带邮箱）；REQUIRE_EMAIL_VERIFICATION 开/关两种情况都兼容
            resp = await client.post(
                f"{API}/auth/register",
                json={
                    "username": USERNAME,
                    "password": PASSWORD,
                    "email": EMAIL,
                    "realName": "冒烟临时用户",
                },
            )
            show("5 register", resp)
            assert resp.status_code == 201, resp.text
            body = resp.json()
            user_id = body["userId"]
            need_verify = body.get("emailVerificationRequired") is True

            if need_verify:
                # 5a) 需激活：注册响应带 emailVerificationRequired=true，
                #     激活令牌已在 Redis（email:activation:user:{uid} → token）
                assert "邮件激活" in body["message"], body
                token = await redis_service.get(f"email:activation:user:{user_id}")
                assert token, "激活令牌未写入 Redis"
                resp = await client.get(
                    f"{API}/auth/verify-email", params={"token": token}
                )
                show("5a verify-email 激活", resp)
                assert resp.status_code == 200, resp.text
                assert resp.json()["message"] == "账户激活成功，请登录", resp.text
                # 5b) 令牌一次性：同一 token 第二次 → 400
                resp = await client.get(
                    f"{API}/auth/verify-email", params={"token": token}
                )
                show("5b 同 token 二次激活", resp)
                assert resp.status_code == 400, resp.text
            else:
                # 5a') 免验证分支：用户已激活，走 service 建令牌验证幂等提示
                from app.services.email_activation import email_activation_service

                token = await email_activation_service.create_token(user_id)
                resp = await client.get(
                    f"{API}/auth/verify-email", params={"token": token}
                )
                show("5a' verify-email 已激活（幂等）", resp)
                assert resp.status_code == 200, resp.text
                assert resp.json()["message"] == "账户已激活，请直接登录", resp.text

            # 6) send-code 未注册邮箱 → 404「该邮箱未注册」（TS 同款明确提示）
            resp = await client.post(
                f"{API}/auth/password/reset/send-code",
                json={"email": f"nobody-{RUN_TAG}@example.com"},
            )
            show("6 send-code 未注册邮箱", resp)
            assert resp.status_code == 404, resp.text
            assert resp.json()["detail"] == "该邮箱未注册", resp.text

            # 7) send-code 正常 → 201；验证码落 Redis（TTL 600s）
            resp = await client.post(
                f"{API}/auth/password/reset/send-code", json={"email": EMAIL}
            )
            show("7 send-code 正常", resp)
            assert resp.status_code == 201, resp.text
            assert resp.json() == {"message": "验证码已发送"}, resp.text
            code = await redis_service.get(reset_code_key(EMAIL))
            assert code and len(code) == 6 and code.isdigit(), f"验证码异常：{code}"

            # 8) 冷却：剩余 TTL > 600-60 → 400「验证码已发送，请稍后再试」
            resp = await client.post(
                f"{API}/auth/password/reset/send-code", json={"email": EMAIL}
            )
            show("8 send-code 冷却期内重发", resp)
            assert resp.status_code == 400, resp.text
            assert resp.json()["detail"] == "验证码已发送，请稍后再试", resp.text

            # 9) 错码 → 400「验证码错误或已过期」（verify 只比对不删，码仍在）
            resp = await client.post(
                f"{API}/auth/password/reset",
                json={"email": EMAIL, "code": "000000", "newPassword": NEW_PASSWORD},
            )
            show("9 reset 错码", resp)
            assert resp.status_code == 400, resp.text
            assert resp.json()["detail"] == "验证码错误或已过期", resp.text

            # 10) 正确验证码 → 201「密码重置成功，请登录」；码一次性删除
            resp = await client.post(
                f"{API}/auth/password/reset",
                json={"email": EMAIL, "code": code, "newPassword": NEW_PASSWORD},
            )
            show("10 reset 正确验证码", resp)
            assert resp.status_code == 201, resp.text
            assert resp.json() == {"message": "密码重置成功，请登录"}, resp.text
            assert await redis_service.get(reset_code_key(EMAIL)) is None, "验证码未删除"

            # 11) 旧密码登录 → 401；新密码登录 → 201
            resp = await client.post(
                f"{API}/auth/login",
                json={"username": USERNAME, "password": PASSWORD},
            )
            show("11a 旧密码登录", resp)
            assert resp.status_code == 401, resp.text
            resp = await client.post(
                f"{API}/auth/login",
                json={"username": USERNAME, "password": NEW_PASSWORD},
            )
            show("11b 新密码登录", resp)
            assert resp.status_code == 201, resp.text

            print("✅ 全部断言通过（12 组）")
    finally:
        await cleanup()
        await redis_service.close()


if __name__ == "__main__":
    asyncio.run(main())
