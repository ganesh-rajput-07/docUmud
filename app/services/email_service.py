import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger("documud.email")


def email_enabled() -> bool:
    return bool(settings.smtp_host)


def send_signing_invite(
    to: str, sender_name: str, filename: str, link: str, expires_days: int
) -> bool:
    """Send the invitation. Returns False (never raises) if SMTP is unconfigured or fails."""
    if not email_enabled():
        return False
    msg = EmailMessage()
    msg["Subject"] = f"{sender_name} asked you to sign “{filename}”"
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg.set_content(
        f"{sender_name} has requested your signature on “{filename}”.\n\n"
        f"Review and sign here:\n{link}\n\n"
        f"This link expires in {expires_days} days. If you weren't expecting this, ignore this email.\n\n"
        f"— {settings.app_name}"
    )
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
        return True
    except Exception:
        logger.exception("Failed to send signing invite to %s", to)
        return False
