from typing import Literal

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import SmtpConfig, User, aware, utc_now
from app.security import get_session_user
from app.services import crypto, mailer

router = APIRouter(prefix="/email", tags=["Email settings"])


class SmtpIn(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, max_length=500, description="Omit to keep the saved password")
    from_email: str = Field(default="", max_length=255)
    from_name: str = Field(default="", max_length=120)
    daily_limit: int = Field(default=400, ge=0, le=100000)


def _view(db: Session, user: User) -> dict:
    cfg = db.scalar(select(SmtpConfig).where(SmtpConfig.user_id == user.id))
    return {
        "source": "user" if cfg else ("platform" if mailer.platform_settings() else "none"),
        "settings": None if not cfg else {
            "host": cfg.host, "port": cfg.port, "security": cfg.security, "username": cfg.username,
            "from_email": cfg.from_email, "from_name": cfg.from_name, "daily_limit": cfg.daily_limit,
            "has_password": bool(cfg.password_enc),
            "verified_at": aware(cfg.verified_at).isoformat() if cfg.verified_at else None,
            "last_error": cfg.last_error,
        },
    }


@router.get("/providers")
def providers(user: User = Depends(get_session_user)):
    return mailer.PRESETS


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), user: User = Depends(get_session_user)):
    return _view(db, user)


@router.put("/settings")
def save_settings(body: SmtpIn, db: Session = Depends(get_db), user: User = Depends(get_session_user)):
    """Save your own mailbox and check the login straight away. The password is stored encrypted
    and is never returned."""
    host = body.host.strip().lower()
    try:
        mailer.validate_target(host, body.port)
    except ValueError as e:
        raise HTTPException(422, str(e))
    from_email = (body.from_email or body.username).strip()
    try:
        from_email = validate_email(from_email, check_deliverability=False).normalized
    except EmailNotValidError:
        raise HTTPException(422, "Enter a valid From email address (or use your full email as the username).")

    cfg = db.scalar(select(SmtpConfig).where(SmtpConfig.user_id == user.id))
    if cfg is None:
        if body.username and not body.password:
            raise HTTPException(422, "Enter the password (for Gmail, an app password).")
        cfg = SmtpConfig(user_id=user.id, host=host, port=body.port, from_email=from_email)
        db.add(cfg)
    cfg.host, cfg.port, cfg.security = host, body.port, body.security
    cfg.username, cfg.from_email, cfg.from_name = body.username.strip(), from_email, body.from_name.strip()
    cfg.daily_limit = body.daily_limit
    if body.password:
        cfg.password_enc = crypto.encrypt(body.password.replace(" ", "") if "gmail" in host or "google" in host else body.password)
    cfg.verified_at, cfg.last_error = None, None

    result = mailer.from_config(cfg)
    try:
        if result is None:
            raise mailer.MailError("The saved password can't be read. Please enter it again.", True)
        mailer.verify_login(result)
        cfg.verified_at = utc_now()
    except mailer.MailError as e:
        cfg.last_error = str(e)
    db.commit()
    return _view(db, user)


@router.post("/test")
def send_test(db: Session = Depends(get_db), user: User = Depends(get_session_user)):
    """Send a test message to your own account's email address using the saved settings."""
    cfg = mailer.resolve(db, user.id)
    if cfg is None:
        raise HTTPException(409, "Save your email settings first.")
    msg = mailer.build_message(
        cfg, user.email, f"{settings.app_name} test email",
        f"This is a test from {settings.app_name}. Signature invitations will be sent from this mailbox.",
        reply_to=user.email,
    )
    record = db.scalar(select(SmtpConfig).where(SmtpConfig.user_id == user.id))
    try:
        mailer.send_one(cfg, msg)
    except mailer.MailError as e:
        if record:
            record.last_error, record.verified_at = str(e), None
            db.commit()
        raise HTTPException(502, str(e))
    if record:
        record.last_error, record.verified_at = None, utc_now()
        db.commit()
    return {"sent_to": user.email, "from": cfg.from_email}


@router.delete("/settings", status_code=204)
def remove_settings(db: Session = Depends(get_db), user: User = Depends(get_session_user)):
    cfg = db.scalar(select(SmtpConfig).where(SmtpConfig.user_id == user.id))
    if cfg:
        db.delete(cfg)
        db.commit()
