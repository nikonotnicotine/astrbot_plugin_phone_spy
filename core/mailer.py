"""通过 SMTP 发送触发邮件到 iCloud 邮箱。

iCloud 邮箱是必需的：非 iCloud 邮箱（如 QQ 邮箱发往 QQ 邮箱）邮件到达
有 5-15 分钟的轮询延迟，会导致 iOS 快捷指令触发延迟。请务必使用 iCloud
邮箱作为收件人。

兼容 aiosmtplib 3.x（设计文档中的原始 API）和 5.x（新版 API）：
- 3.x: send(dict_message, hostname=..., port=..., username=..., password=..., use_tls=...)
- 5.x: send(EmailMessage, hostname=..., port=..., username=..., password=..., use_tls=...)

本模块使用标准库 EmailMessage 构造消息，两种版本都支持。
"""
from __future__ import annotations

from email.message import EmailMessage

import aiosmtplib
from astrbot.api import logger

# 触发邮件的主题和正文。iOS 快捷指令的邮件自动化按主题包含关键字触发。
TRIGGER_SUBJECT = "PHONESPY_TRIGGER"
TRIGGER_BODY = "Screenshot request triggered."


async def send_trigger_email(
    host: str,
    port: int,
    username: str,
    password: str,
    to_address: str,
    subject: str = TRIGGER_SUBJECT,
    body: str = TRIGGER_BODY,
) -> None:
    """向 to_address 发送一封触发邮件。

    根据端口选择连接方式：
    - 465: SSL 直连 (use_tls=True)
    - 587: STARTTLS 加密 (start_tls=True)

    参数:
        subject: 邮件主题，默认为 TRIGGER_SUBJECT
        body: 邮件正文，默认为 TRIGGER_BODY
    """
    message = EmailMessage()
    message["From"] = username
    message["To"] = [to_address]
    message["Subject"] = subject
    message.set_content(body)

    use_tls = port == 465

    try:
        await aiosmtplib.send(
            message,
            hostname=host,
            port=port,
            username=username,
            password=password,
            use_tls=use_tls,
            start_tls=not use_tls,
            timeout=15,
        )
        logger.info(
            f"📧 触发邮件已发送到 iCloud 邮箱 {to_address}（SMTP {host}:{port}）"
        )
    except aiosmtplib.SMTPException as e:
        logger.error(f"❌ 触发邮件发送失败 (SMTP 错误): {type(e).__name__} - {e}")
        raise
    except Exception as e:
        logger.error(f"❌ 触发邮件发送失败: {type(e).__name__} - {e}")
        raise
