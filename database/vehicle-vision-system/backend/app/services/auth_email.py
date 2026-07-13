import smtplib
from email.message import EmailMessage

from app.config import settings


class EmailDeliveryError(RuntimeError):
    """Raised when an authentication email cannot be delivered."""


def send_verification_email(recipient: str, code: str, purpose: str) -> None:
    """Send a one-time authentication code through the configured SMTP server."""
    if not all((settings.smtp_host.strip(), settings.smtp_user.strip(), settings.smtp_password)):
        raise EmailDeliveryError("邮件服务尚未配置，请联系管理员")

    action = "注册账号" if purpose == "register" else "登录系统"
    message = EmailMessage()
    message["Subject"] = f"{settings.app_name} - {action}验证码"
    message["From"] = settings.smtp_user
    message["To"] = recipient
    message.set_content(
        f"您正在{action}，验证码为：{code}\n\n"
        "验证码 5 分钟内有效，请勿转发给他人。若非本人操作，请忽略此邮件。"
    )

    try:
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout) as server:
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(message)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout) as server:
                server.ehlo()
                if settings.smtp_use_tls:
                    server.starttls()
                    server.ehlo()
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError("验证码邮件发送失败，请稍后重试") from exc
