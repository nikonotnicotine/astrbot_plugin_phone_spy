import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


async def send_trigger_email(config: dict):
    """
    发送触发邮件到 iCloud，触发 iOS 快捷指令自动截屏
    """
    msg = MIMEMultipart()
    msg["From"] = config["smtp_user"]
    msg["To"] = config["icloud_address"]
    msg["Subject"] = "PHONESPY_TRIGGER"
    msg.attach(MIMEText("Screenshot request triggered.", "plain"))

    use_tls = config.get("smtp_port", 465) == 465

    await aiosmtplib.send(
        msg,
        hostname=config["smtp_host"],
        port=config.get("smtp_port", 465),
        username=config["smtp_user"],
        password=config["smtp_password"],
        use_tls=use_tls,
        start_tls=not use_tls,
    )
