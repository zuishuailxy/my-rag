"""
邮件发送服务

Python 侧用 aiosmtplib + 标准库 EmailMessage（multipart/alternative：纯文本 + HTML），
邮件文案（激活链接 24 小时有效 / 重置验证码 10 分钟有效）。

- 激活链接格式：{APP_PUBLIC_URL}/auth/verify-email?token={token}
- 发送失败不吞错：SMTP 异常原样抛出，由调用方（注册流程等）决定处理方式，
"""
import logging
from email.message import EmailMessage

import aiosmtplib

from app.core.config import settings

logger = logging.getLogger("EmailService")


class EmailService:
    """SMTP 邮件发送（单例；SMTP 参数每次发送时从 settings 读取）"""

    # ---------- 业务邮件 ----------

    async def send_activation_email(
        self, email: str, username: str, token: str
    ) -> None:
        """注册激活邮件：链接 24 小时内有效（TTL 在 EmailActivationService 侧控制）"""
        base_url = settings.APP_PUBLIC_URL
        link = f"{base_url}/auth/verify-email?token={token}"
        subject = "激活您的知识库账户"
        text = (
            f"您好 {username}，请点击以下链接激活账户（24 小时内有效）：\n{link}"
        )
        html = f"""
<p>您好 <strong>{username}</strong>，</p>
<p>欢迎注册知识库，请点击下方链接激活账户（24 小时内有效）：</p>
<p><a href="{link}">{link}</a></p>
<p>如非本人操作，请忽略此邮件。</p>
"""
        await self._send(self._build_message(email, subject, text, html))
        logger.info("激活邮件已发送：to=%s, username=%s", email, username)

    async def send_reset_code_email(
        self, email: str, username: str, code: str
    ) -> None:
        """密码重置验证码邮件：10 分钟内有效（TTL 在验证码服务侧控制）"""
        subject = "密码重置验证码"
        text = (
            f"您好 {username}，您的密码重置验证码是：{code}，"
            f"10 分钟内有效，请勿泄露。"
        )
        html = f"""
<p>您好 <strong>{username}</strong>，</p>
<p>您正在重置密码，验证码为：</p>
<p style="font-size:24px;font-weight:bold;letter-spacing:4px;">{code}</p>
<p>验证码 10 分钟内有效，请勿泄露给他人。</p>
<p>如非本人操作，请忽略此邮件。</p>
"""
        await self._send(self._build_message(email, subject, text, html))
        logger.info("重置验证码邮件已发送：to=%s, username=%s", email, username)

    # ---------- 内部实现 ----------

    def _build_message(
        self, to: str, subject: str, text: str, html: str
    ) -> EmailMessage:
        """组装 multipart/alternative 邮件"""
        message = EmailMessage()
        message["From"] = settings.MAIL_FROM
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        return message

    async def _send(self, message: EmailMessage) -> None:
        """经 SMTP 发送；失败原样抛出"""
        # MAIL_SECURE=true → 隐式 TLS（465）；false → STARTTLS（587），nodemailer 同义
        await aiosmtplib.send(
            message,
            hostname=settings.MAIL_HOST,
            port=settings.MAIL_PORT,
            username=settings.MAIL_USER or None,
            password=settings.MAIL_PASS or None,
            use_tls=settings.MAIL_SECURE,
            start_tls=not settings.MAIL_SECURE,
        )


# 全局单例
email_service = EmailService()
