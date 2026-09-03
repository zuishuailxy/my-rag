"""注册邮箱验证分叉 + 密码重置 冒烟验证（E2E）

覆盖 auth.service.ts 的 register 分叉 / verifyEmail / sendResetCode /
resetPasswordByEmail 全链路（对应 TS 行为逐条断言）：

- 注册分叉：REQUIRE_EMAIL_VERIFICATION=true 且带邮箱 → 建未激活用户
  （status=0 / email_verified=0）+ 发激活邮件（猴子补丁捕获，不真发 SMTP）；
  免验证路径 → 立即可登录，响应无 emailVerificationRequired 键
- 激活：无效 token 400；有效 token 恢复 status/email_verified；幂等再激活
- 密码重置：发送（冷却拦截）→ 错码 400 → 正确码重置 → 新旧密码登录验证
- 邮箱查重：409「邮箱已被使用」

⚠️ TS 保真点：未激活用户登录报「账户已禁用」而非「账户未激活」——
TS register 把未激活用户 status 也置 0，validateCredentials 的 status 检查
先于 email_verified 检查命中。本脚本按 TS 顺序锁定该行为。

前置条件：docker 的 PostgreSQL + Redis 运行中。脚本自建测试用户并在结束时
硬删清理（含 user_role），Redis 键按前缀清理，不污染种子数据。

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_email_flow.py
"""

import asyncio
import logging
import re
from email.message import EmailMessage

from fastapi import HTTPException
from sqlalchemy import delete, select

from app.core.config import settings
from app.db.redis import redis_service
from app.db.session import async_session_factory
from app.models.user import User
from app.models.user_role import UserRole
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services.auth import auth_service
from app.services.email import email_service
from app.services.email_activation import USER_PREFIX, email_activation_service
from app.services.password_reset import RESET_CODE_PREFIX

logging.basicConfig(level=logging.WARNING, format="%(levelname)s [%(name)s] %(message)s")

# 捕获到的邮件
_sent: list[EmailMessage] = []

# 测试用户（脚本创建，结束硬删清理）
_users: list[int] = []

VERIFY_EMAIL = "smoke-flow@example.com"
FREE_EMAIL = "smoke-flow-2@example.com"


def _part_of(message: EmailMessage, content_type: str) -> str:
    for part in message.iter_parts():
        if part.get_content_type() == content_type:
            return str(part.get_content())
    return ""


def ok(step: str) -> None:
    print(f"  ✅ {step}")


async def _expect_http(step: str, coro, status: int, detail: str) -> None:
    """断言协程抛出指定 status + detail 的 HTTPException"""
    try:
        await coro
    except HTTPException as e:
        assert e.status_code == status, f"{step}: {e.status_code} != {status}"
        assert e.detail == detail, f"{step}: {e.detail!r} != {detail!r}"
        return
    raise AssertionError(f"{step}: 应抛 {status} {detail}，但未抛错")


async def _register(
    session, username: str, email: str | None = None
) -> tuple:
    """经 auth_service 注册并取回落库实体（记录 ID 供清理）"""
    dto = RegisterRequest(
        username=username,
        password="oldpass123",
        email=email,  # type: ignore[arg-type]
        real_name="冒烟测试",
    )
    result = await auth_service.register(session, dto)
    user = (
        await session.scalars(select(User).where(User.username == username))
    ).first()
    assert user is not None
    _users.append(int(user.id))
    return result, user


async def _cleanup() -> None:
    """硬删脚本创建的用户 + 角色关联 + Redis 键（冒烟不污染）"""
    async with async_session_factory() as session:
        if _users:
            await session.execute(delete(UserRole).where(UserRole.user_id.in_(_users)))
            await session.execute(delete(User).where(User.id.in_(_users)))
            await session.commit()
    client = redis_service.client
    assert client is not None
    keys: list[str] = []
    for pattern in ("email:activation:*", "password:reset:code:*"):
        keys.extend([k async for k in client.scan_iter(match=pattern)])
    if keys:
        await redis_service.delete(*keys)
        print(f"  🧹 已清理 Redis 键 {len(keys)} 个")


async def main() -> bool:
    await redis_service.start()

    async def _capture(message: EmailMessage) -> None:
        _sent.append(message)

    orig_send = email_service._send
    orig_require = settings.REQUIRE_EMAIL_VERIFICATION
    email_service._send = _capture  # type: ignore[method-assign]
    try:
        # ============ 一、注册分叉（REQUIRE_EMAIL_VERIFICATION=true） ============
        settings.REQUIRE_EMAIL_VERIFICATION = True
        print("—— 注册邮箱验证分叉 ——")
        async with async_session_factory() as session:
            # 1) 需验证注册：未激活用户 + 激活邮件
            result, user = await _register(session, "smoke-flow-verify", VERIFY_EMAIL)
            assert result.email_verification_required is True
            assert result.message == "注册成功，请查收邮件激活账户"
            assert user.status == 0 and user.email_verified == 0
            ok("需验证注册：status=0 / email_verified=0，返回 emailVerificationRequired")

            # 2) 激活邮件内容 + 双 key 落 Redis
            msg = _sent[-1]
            assert msg["To"] == VERIFY_EMAIL
            assert msg["Subject"] == "激活您的知识库账户"
            text = _part_of(msg, "text/plain")
            match = re.search(r"token=([0-9a-f]{64})", text)
            assert match is not None, text
            token = match.group(1)
            assert await redis_service.get(f"{USER_PREFIX}{user.id}") == token
            ok("激活邮件：链接含 64 位 hex token，Redis 双 key 落库")

            # 3) 未激活登录 → 401（TS 保真：status 检查先命中，报「账户已禁用」）
            await _expect_http(
                "未激活登录",
                auth_service.login(
                    session, LoginRequest(username="smoke-flow-verify", password="oldpass123")
                ),
                401,
                "账户已禁用",
            )
            ok("未激活登录被拦（401 账户已禁用——TS 检查顺序保真）")

            # 4) 无效 token → 400
            await _expect_http(
                "无效激活",
                auth_service.verify_email(session, "deadbeef" * 8),
                400,
                "激活链接无效或已过期",
            )
            ok("无效 token 激活被拦（400 激活链接无效或已过期）")

            # 5) 有效 token 激活 → 双字段恢复
            message = (await auth_service.verify_email(session, token)).message
            await session.refresh(user)
            assert message == "账户激活成功，请登录"
            assert user.status == 1 and user.email_verified == 1
            ok("激活成功：status=1 / email_verified=1")

            # 6) 幂等：再次激活 → 提示已激活
            again = await email_activation_service.create_token(str(user.id))
            message2 = (await auth_service.verify_email(session, again)).message
            assert message2 == "账户已激活，请直接登录"
            ok("重复激活幂等（账户已激活，请直接登录）")

            # 7) 激活后可登录
            login = await auth_service.login(
                session, LoginRequest(username="smoke-flow-verify", password="oldpass123")
            )
            assert login.user_info.username == "smoke-flow-verify"
            ok("激活后登录成功")

        # ============ 二、免验证注册（默认路径） ============
        settings.REQUIRE_EMAIL_VERIFICATION = False
        print("—— 免验证注册 + 邮箱查重 ——")
        async with async_session_factory() as session:
            result, user2 = await _register(session, "smoke-flow-free", FREE_EMAIL)
            assert result.email_verification_required is None, "免验证路径不应带该键"
            assert result.message == "注册成功，请登录"
            assert user2.status == 1 and user2.email_verified == 1
            ok("免验证注册：status=1 / email_verified=1，无 emailVerificationRequired")

            # 8) 邮箱查重 → 409（用户名不同、邮箱相同）
            await _expect_http(
                "邮箱查重",
                _register(session, "smoke-flow-dup", FREE_EMAIL),
                409,
                "邮箱已被使用",
            )
            ok("重复邮箱注册被拦（409 邮箱已被使用）")

            # ============ 三、密码重置全链路 ============
            print("—— 密码重置 ——")
            # 9) 未注册邮箱 → 404
            await _expect_http(
                "未注册邮箱",
                auth_service.send_reset_code(session, "nobody@example.com"),
                404,
                "该邮箱未注册",
            )
            ok("未注册邮箱发码被拦（404 该邮箱未注册）")

            # 10) 发码：验证码 6 位 + TTL 生效 + 邮件内容
            message3 = (
                await auth_service.send_reset_code(session, FREE_EMAIL)
            ).message
            assert message3 == "验证码已发送"
            ttl = await redis_service.ttl(f"{RESET_CODE_PREFIX}{FREE_EMAIL}")
            assert 0 < ttl <= 600, ttl
            msg = _sent[-1]
            assert msg["Subject"] == "密码重置验证码"
            match = re.search(r"验证码是：(\d{6})", _part_of(msg, "text/plain"))
            assert match is not None
            code = match.group(1)
            ok(f"验证码已发送：{code}，Redis TTL={ttl}s")

            # 11) 冷却：60s 内重发 → 400
            await _expect_http(
                "冷却重发",
                auth_service.send_reset_code(session, FREE_EMAIL),
                400,
                "验证码已发送，请稍后再试",
            )
            ok("60 秒冷却内重发被拦")

            # 12) 错码 → 400
            wrong = "000000" if code != "000000" else "111111"
            await _expect_http(
                "错误验证码",
                auth_service.reset_password_by_email(session, FREE_EMAIL, wrong, "newpass456"),
                400,
                "验证码错误或已过期",
            )
            ok("错误验证码重置被拦")

            # 13) 正确验证码 → 重置成功 + 删码；旧密码失效、新密码可登录
            message4 = (
                await auth_service.reset_password_by_email(
                    session, FREE_EMAIL, code, "newpass456"
                )
            ).message
            assert message4 == "密码重置成功，请登录"
            assert await redis_service.get(f"{RESET_CODE_PREFIX}{FREE_EMAIL}") is None
            await _expect_http(
                "旧密码登录",
                auth_service.login(
                    session, LoginRequest(username="smoke-flow-free", password="oldpass123")
                ),
                401,
                "用户名或密码错误",
            )
            login2 = await auth_service.login(
                session, LoginRequest(username="smoke-flow-free", password="newpass456")
            )
            assert login2.user_info.username == "smoke-flow-free"
            ok("密码重置生效：旧密码 401、新密码登录成功、验证码已删")
    finally:
        email_service._send = orig_send  # type: ignore[method-assign]
        settings.REQUIRE_EMAIL_VERIFICATION = orig_require
        await _cleanup()
        await redis_service.close()

    print("\n全部通过 ✅")
    return True


if __name__ == "__main__":
    passed = asyncio.run(main())
    raise SystemExit(0 if passed else 1)
