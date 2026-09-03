"""邮件服务 + 激活令牌 冒烟验证

覆盖两块（对应 TS email.service.ts / email-activation.service.ts）：
- EmailService：不真发 SMTP——猴子补丁捕获 _send 的 EmailMessage，校验
  From/To/Subject、纯文本与 HTML 双部分、激活链接格式（文案与 TS 逐字一致）
- EmailActivationService：走真实 Redis 全流程——双 key 设计（token→userId、
  userId→token）、重发作废旧令牌、消费一次性、按 token 作废

前置条件：docker 的 redis 运行中（knowledge_hub_redis）。冒烟键前缀
email:activation: 内部自清理。

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_email_services.py
"""
import asyncio
import logging
from email.message import EmailMessage

from app.db.redis import redis_service
from app.services.email import email_service
from app.services.email_activation import (
    ACTIVATION_TOKEN_TTL_SECONDS,
    TOKEN_PREFIX,
    USER_PREFIX,
    email_activation_service,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s [%(name)s] %(message)s")

# 捕获到的邮件
_sent: list[EmailMessage] = []


def _part_of(message: EmailMessage, content_type: str) -> str:
    """从 multipart/alternative 中取指定类型的子部分内容"""
    for part in message.iter_parts():
        if part.get_content_type() == content_type:
            return str(part.get_content())
    return ""


def ok(step: str) -> None:
    print(f"  ✅ {step}")


async def main() -> bool:
    all_ok = True
    await redis_service.start()

    # ============ 一、EmailService（捕获消息，不发 SMTP） ============
    print("—— EmailService ——")

    async def _capture(message: EmailMessage) -> None:
        _sent.append(message)

    orig_send = email_service._send
    email_service._send = _capture  # type: ignore[method-assign]
    try:
        # ---------- 1) 激活邮件：收发件人 / 主题 / 链接 ----------
        await email_service.send_activation_email("u@example.com", "小明", "tok-abc")
        msg = _sent[-1]
        assert msg["To"] == "u@example.com" and msg["From"] is not None
        assert msg["Subject"] == "激活您的知识库账户"
        # 激活链接基址从 settings 取（.env 配置了 APP_PUBLIC_URL，硬编码会误报）
        from app.core.config import settings

        link = f"{settings.APP_PUBLIC_URL}/auth/verify-email?token=tok-abc"
        text = _part_of(msg, "text/plain")
        html = _part_of(msg, "text/html")
        assert "小明" in text and link in text, text
        assert f'<a href="{link}">{link}</a>' in html and "24 小时内有效" in html, html
        ok("激活邮件：Subject/链接/双部分文案与 TS 一致")

        # ---------- 2) 重置验证码邮件 ----------
        await email_service.send_reset_code_email("u@example.com", "小明", "654321")
        msg = _sent[-1]
        assert msg["Subject"] == "密码重置验证码"
        text = _part_of(msg, "text/plain")
        html = _part_of(msg, "text/html")
        assert "654321" in text and "10 分钟内有效" in text, text
        assert "654321" in html and "letter-spacing:4px" in html, html
        ok("重置验证码邮件：验证码/有效期文案与 TS 一致")
    finally:
        email_service._send = orig_send  # type: ignore[method-assign]

    # ============ 二、EmailActivationService（真实 Redis E2E） ============
    print("—— EmailActivationService ——")
    user_id = "1000000000000000001"  # 种子 admin，不落 PG，只碰 Redis
    try:
        # ---------- 3) 创建令牌：64 hex + 双 key 落库 ----------
        token1 = await email_activation_service.create_token(user_id)
        assert len(token1) == 64 and all(c in "0123456789abcdef" for c in token1), token1
        assert await redis_service.get(f"{TOKEN_PREFIX}{token1}") == user_id
        assert await redis_service.get(f"{USER_PREFIX}{user_id}") == token1
        ttl = await redis_service.ttl(f"{TOKEN_PREFIX}{token1}")
        assert 0 < ttl <= ACTIVATION_TOKEN_TTL_SECONDS, ttl
        ok(f"create_token：64 位 hex + 双 key（ttl={ttl}）")

        # ---------- 4) 重发：旧令牌作废、新令牌有效 ----------
        token2 = await email_activation_service.create_token(user_id)
        assert token2 != token1
        assert await redis_service.get(f"{TOKEN_PREFIX}{token1}") is None, "旧 token key 应已删除"
        assert await email_activation_service.consume_token(token1) is None, "旧令牌应已失效"
        assert await redis_service.get(f"{TOKEN_PREFIX}{token2}") == user_id
        assert await redis_service.get(f"{USER_PREFIX}{user_id}") == token2
        ok("重发令牌：旧的被作废，userKey 指向新令牌")

        # ---------- 5) 消费：一次性，两侧 key 一起删 ----------
        got = await email_activation_service.consume_token(token2)
        assert got == user_id
        assert await redis_service.get(f"{TOKEN_PREFIX}{token2}") is None
        assert await redis_service.get(f"{USER_PREFIX}{user_id}") is None
        assert await email_activation_service.consume_token(token2) is None
        ok("consume_token：返回 userId 且二次消费 → None")

        # ---------- 6) delete_by_token：主动作废 + 未知 token 静默 ----------
        token3 = await email_activation_service.create_token(user_id)
        await email_activation_service.delete_by_token(token3)
        assert await redis_service.get(f"{TOKEN_PREFIX}{token3}") is None
        assert await redis_service.get(f"{USER_PREFIX}{user_id}") is None
        await email_activation_service.delete_by_token("no-such-token")  # 不抛错
        ok("delete_by_token：两侧 key 一起删 + 未知 token 静默跳过")
    finally:
        # 清理该前缀全部键（冒烟不污染）
        client = redis_service.client
        assert client is not None
        keys = [
            k
            async for k in client.scan_iter(match="email:activation:*")
        ]
        if keys:
            await redis_service.delete(*keys)
            print(f"  🧹 已清理激活令牌键 {len(keys)} 个")
        await redis_service.close()

    print("\n全部通过 ✅")
    return all_ok


if __name__ == "__main__":
    passed = asyncio.run(main())
    raise SystemExit(0 if passed else 1)
