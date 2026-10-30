"""Sending email through the *sender's own* SMTP account (Gmail app password, Outlook, any server),
falling back to a platform-wide SMTP configured in .env."""
import html
import ipaddress
import logging
import re
import smtplib
import socket
import ssl
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from sqlalchemy.orm import Session

from app.config import settings
from app.models import SmtpConfig
from app.services import crypto

logger = logging.getLogger("documud.mailer")

ALLOWED_PORTS = {25, 465, 587, 2525}
TIMEOUT = 20

PRESETS = [
    {"id": "gmail", "label": "Gmail / Google Workspace", "host": "smtp.gmail.com", "port": 587, "security": "starttls", "daily_limit": 400,
     "help": "Turn on 2-Step Verification, then create an App Password at myaccount.google.com/apppasswords and paste the 16 characters (no spaces). Gmail allows about 500 emails a day (2,000 on Workspace)."},
    {"id": "outlook", "label": "Outlook / Microsoft 365", "host": "smtp.office365.com", "port": 587, "security": "starttls", "daily_limit": 400,
     "help": "Use your full email as the username. Your admin must allow SMTP AUTH for the mailbox; with MFA use an app password."},
    {"id": "zoho", "label": "Zoho Mail", "host": "smtp.zoho.com", "port": 465, "security": "ssl", "daily_limit": 400,
     "help": "Use an app-specific password from Zoho account security. India accounts use smtp.zoho.in."},
    {"id": "yahoo", "label": "Yahoo Mail", "host": "smtp.mail.yahoo.com", "port": 465, "security": "ssl", "daily_limit": 400,
     "help": "Generate an app password under Account security."},
    {"id": "sendgrid", "label": "SendGrid", "host": "smtp.sendgrid.net", "port": 587, "security": "starttls", "daily_limit": 0,
     "help": "Username is the word apikey; the password is your SendGrid API key. The From address must be a verified sender."},
    {"id": "ses", "label": "Amazon SES", "host": "email-smtp.us-east-1.amazonaws.com", "port": 587, "security": "starttls", "daily_limit": 0,
     "help": "Use SES SMTP credentials (not your AWS login). Change the region in the server name if needed; the From address must be verified."},
    {"id": "brevo", "label": "Brevo (Sendinblue)", "host": "smtp-relay.brevo.com", "port": 587, "security": "starttls", "daily_limit": 0,
     "help": "Use your Brevo login and an SMTP key as the password."},
    {"id": "custom", "label": "Other / custom SMTP", "host": "", "port": 587, "security": "starttls", "daily_limit": 0,
     "help": "Enter the server details from your email provider or IT team."},
]


class MailError(Exception):
    """A send/login failure with a message safe to show to users."""

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass
class SmtpSettings:
    host: str
    port: int
    security: str
    username: str
    password: str
    from_email: str
    from_name: str
    daily_limit: int
    source: str  # "user" | "platform"


def validate_target(host: str, port: int) -> None:
    """Refuse hosts that would turn the server into a probe of internal networks."""
    if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host or ""):
        raise ValueError("Enter a valid SMTP server name, for example smtp.gmail.com.")
    if settings.environment == "development":
        return
    if port not in ALLOWED_PORTS:
        raise ValueError("Use one of the standard SMTP ports: 25, 465, 587 or 2525.")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError("That SMTP server name could not be found.")
    if any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise ValueError("The SMTP server must be a public host.")


def platform_settings() -> SmtpSettings | None:
    if not settings.smtp_host:
        return None
    return SmtpSettings(settings.smtp_host, settings.smtp_port, settings.smtp_security, settings.smtp_user,
                        settings.smtp_password, settings.smtp_from, settings.app_name, 0, "platform")


def from_config(cfg: SmtpConfig) -> SmtpSettings | None:
    password = crypto.decrypt(cfg.password_enc) if cfg.password_enc else ""
    if password is None:
        logger.warning("SMTP password for user %s can't be decrypted (key changed?)", cfg.user_id)
        return None
    return SmtpSettings(cfg.host, cfg.port, cfg.security, cfg.username, password,
                        cfg.from_email, cfg.from_name, cfg.daily_limit, "user")


def resolve(db: Session, user_id: str) -> SmtpSettings | None:
    """The sender's own mailbox if they set one up, else the platform default, else None."""
    cfg = db.query(SmtpConfig).filter(SmtpConfig.user_id == user_id).first()
    if cfg and (own := from_config(cfg)):
        return own
    return platform_settings()


def friendly(exc: Exception, host: str = "", port: int = 0) -> MailError:
    where = f"{host}:{port}" if host else "the mail server"
    if isinstance(exc, MailError):
        return exc
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return MailError("Login failed: the server rejected the username or password. Gmail, Yahoo and Outlook need an app password, not your normal password.", True)
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return MailError("The server refused the From address. Use an address that belongs to this account.", True)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return MailError("The recipient address was refused by the server.", True)
    if isinstance(exc, ssl.SSLError):
        return MailError(f"Secure connection to {where} failed. Check the port and the security setting (STARTTLS vs SSL/TLS).")
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return MailError("The server doesn't support the selected security or login method.", True)
    if isinstance(exc, smtplib.SMTPResponseException):
        text = (exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error))[:140]
        return MailError(f"The server replied {exc.smtp_code}: {text}", 500 <= exc.smtp_code < 600)
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return MailError(f"Timed out connecting to {where}.")
    if isinstance(exc, (OSError, smtplib.SMTPException)):
        return MailError(f"Could not connect to {where}. Check the server name and port.")
    return MailError("Unexpected email error.")


def _connect(cfg: SmtpSettings) -> smtplib.SMTP:
    try:
        if cfg.security == "ssl":
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=TIMEOUT, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=TIMEOUT)
            smtp.ehlo()
            if cfg.security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
        if cfg.username:
            smtp.login(cfg.username, cfg.password)
        return smtp
    except Exception as exc:
        raise friendly(exc, cfg.host, cfg.port) from exc


@contextmanager
def session(cfg: SmtpSettings):
    """One authenticated connection, reused for several messages."""
    smtp = _connect(cfg)
    try:
        yield smtp
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def verify_login(cfg: SmtpSettings) -> None:
    with session(cfg):
        pass


def send(smtp: smtplib.SMTP, cfg: SmtpSettings, msg: EmailMessage) -> None:
    try:
        smtp.send_message(msg)
    except Exception as exc:
        raise friendly(exc, cfg.host, cfg.port) from exc


# ---------------- message building ----------------

def build_message(cfg: SmtpSettings, to: str, subject: str, text: str, html_body: str | None = None,
                  reply_to: str | None = None, attachments: list[tuple[str, bytes, str]] = ()) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = re.sub(r"[\r\n]+", " ", subject)[:200]
    msg["From"] = formataddr((cfg.from_name, cfg.from_email)) if cfg.from_name else cfg.from_email
    msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for filename, data, mime in attachments:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def fill_placeholders(template: str, **values: str) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def _html(title: str, lines: list[str], button: tuple[str, str] | None, footer: str) -> str:
    e = html.escape
    body = "".join(f'<p style="margin:0 0 14px;line-height:1.55">{e(line)}</p>' for line in lines)
    btn = (f'<p style="margin:22px 0"><a href="{e(button[1])}" style="background:#4f46e5;color:#fff;text-decoration:none;'
           f'padding:12px 22px;border-radius:8px;font-weight:600;display:inline-block">{e(button[0])}</a></p>') if button else ""
    return (f'<div style="font-family:Arial,Helvetica,sans-serif;color:#0f172a;max-width:560px;margin:0 auto;padding:24px">'
            f'<h2 style="margin:0 0 16px;font-size:20px">{e(title)}</h2>{body}{btn}'
            f'<hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0"><p style="color:#64748b;font-size:12px;margin:0">{e(footer)}</p></div>')


def invite_email(*, sender: str, filename: str, link: str, days: int, recipient: str = "", custom: str = "",
                 subject: str = "", reminder: bool = False) -> tuple[str, str, str]:
    default_subject = f"{'Reminder: ' if reminder else ''}{sender} asked you to sign “{filename}”"
    subject = fill_placeholders(subject, name=recipient, document=filename) if subject.strip() else default_subject
    lines = [f"Hello{' ' + recipient if recipient else ''},",
             f"{sender} has {'reminded you about' if reminder else 'asked for'} your signature on “{filename}”."]
    if custom.strip():
        lines.append(fill_placeholders(custom.strip(), name=recipient, document=filename))
    lines.append(f"This secure link expires in {days} day{'s' if days != 1 else ''}.")
    footer = f"Sent by {settings.app_name} on behalf of {sender}. If you weren't expecting this, you can ignore this email."
    text = "\n\n".join(lines) + f"\n\nReview and sign:\n{link}\n\n— {footer}"
    return subject, text, _html("Please review and sign", lines, ("Review & sign", link), footer)


def signed_email(*, sender: str, signer: str, filename: str, to_signer: bool) -> tuple[str, str, str]:
    if to_signer:
        subject = f"Your signed copy of “{filename}”"
        lines = [f"Hello {signer},", f"Thank you for signing “{filename}”. Your signed copy is attached for your records."]
    else:
        subject = f"{signer} signed “{filename}”"
        lines = [f"{signer} has signed “{filename}”.", "The signed copy is attached."]
    footer = f"Sent by {settings.app_name}."
    return subject, "\n\n".join(lines) + f"\n\n— {footer}", _html(subject, lines, None, footer)


def otp_email(*, sender: str, filename: str, code: str, minutes: int) -> tuple[str, str, str]:
    subject = f"Your verification code: {code}"
    lines = [f"Use this code to open “{filename}” sent by {sender}:", f"{code[:3]} {code[3:]}",
             f"It expires in {minutes} minutes. If you didn't request it, ignore this email."]
    footer = f"Sent by {settings.app_name} on behalf of {sender}."
    return subject, "\n\n".join(lines) + f"\n\n— {footer}", _html("Verify your email", lines, None, footer)


def send_one(cfg: SmtpSettings, msg: EmailMessage) -> None:
    with session(cfg) as smtp:
        send(smtp, cfg, msg)
