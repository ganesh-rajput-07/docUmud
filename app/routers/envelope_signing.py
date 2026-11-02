"""Public signing endpoints for envelope recipients. The token in the URL is the credential."""
import hashlib
import hmac
import json
import logging
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Document, Envelope, EnvelopeRecipient, RecipientSession, User, aware, utc_now
from app.services import envelope_service as svc
from app.services import mailer
from app.services.access import read_upload
from app.services.fill_service import FillReport
from app.services.render import draw_fields, finish
from app.services.storage import resolve
from app.services.template_service import font_resolver, jl

router = APIRouter(prefix="/v1/sign", tags=["Recipient signing"])
logger = logging.getLogger("documud.envelope_signing")


def find(token: str, db: Session) -> tuple[EnvelopeRecipient, Envelope]:
    rec = db.scalar(select(EnvelopeRecipient).where(EnvelopeRecipient.token_hash == svc.hash_token(token)))
    if not rec or rec.kind == "cc":
        raise HTTPException(404, "Signing link not found")
    env = db.get(Envelope, rec.envelope_id)
    if env.status == "in_progress" and aware(env.expires_at) <= utc_now():
        svc.expire_due(db)
        db.refresh(env)
    return rec, env


def otp_required(db: Session, env: Envelope, rec: EnvelopeRecipient) -> bool:
    return (settings.require_signer_otp and rec.kind == "signer" and rec.verify and bool(rec.email)
            and mailer.resolve(db, env.user_id) is not None)


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:1]}{'*' * max(len(name) - 1, 1)}@{domain}"


def _code_hash(rid: str, code: str) -> str:
    return hmac.new(settings.secret_key.encode(), f"envotp:{rid}:{code}".encode(), hashlib.sha256).hexdigest()


def _verified(db: Session, env: Envelope, rec: EnvelopeRecipient, token: str | None) -> bool:
    if not otp_required(db, env, rec):
        return True
    return bool(token and db.scalar(select(RecipientSession.id).where(
        RecipientSession.recipient_id == rec.id, RecipientSession.access_hash == svc.hash_token(token),
        RecipientSession.verified_at.is_not(None))))


def _need_verified(db, env, rec, token) -> None:
    if not _verified(db, env, rec, token):
        raise HTTPException(403, "Please verify your email address to continue.")


def _editable(db: Session, rec: EnvelopeRecipient) -> list:
    return [f for f in svc.env_fields(db, rec.envelope_id) if f.recipient_id == rec.id and not f.locked
            and f.type not in svc.AUTO_TYPES and not f.sig_path]


def _vt(v: str | None, x_verify: str | None) -> str | None:
    return x_verify or v


@router.get("/{token}", summary="What this recipient needs to do")
def info(token: str, v: str | None = Query(None), x_verify: str | None = Header(None), db: Session = Depends(get_db)):
    rec, env = find(token, db)
    owner = db.get(User, env.user_id)
    needs_code = otp_required(db, env, rec)
    out = {"envelope": {"id": env.id, "name": env.name, "status": env.status, "message": env.message},
           "sender": owner.name or owner.email,
           "recipient": {"id": rec.id, "role": rec.role, "name": rec.name, "email": rec.email, "status": rec.status},
           "otp_required": needs_code, "email_hint": _mask(rec.email) if needs_code else "",
           "verified": _verified(db, env, rec, _vt(v, x_verify)), "return_url": env.return_url,
           "can_sign": env.status == "in_progress" and rec.status in ("pending", "viewed")}
    if not out["verified"]:
        return out
    doc = db.get(Document, env.document_id)
    out["pages"] = doc.pages
    out["download"] = env.status == "completed"
    if out["can_sign"]:
        if rec.status == "pending":
            rec.status, rec.viewed_at = "viewed", utc_now()
            svc.add_event(db, env, "viewed", f"role={rec.role}", rec.id)
            db.commit()
        out["fields"] = [{"id": f.id, "name": f.name, "label": f.label, "type": f.type, "page": f.page, "x": f.x, "y": f.y,
                          "w": f.w, "h": f.h, "required": f.required, "options": jl(f.options, []), "option": f.option,
                          "value": f.value, "style": {k: v2 for k, v2 in jl(f.style, {}).items() if k in ("align", "font_size", "uppercase")}}
                         for f in _editable(db, rec)]
        out["auto_fields"] = sum(1 for f in svc.env_fields(db, env.id) if f.recipient_id == rec.id and f.type in svc.AUTO_TYPES)
    elif rec.status == "waiting":
        out["waiting_for"] = [r.role for r in svc.active_recipients(svc.env_recipients(db, env.id), env.signing_order)]
    return out


@router.post("/{token}/otp/send", summary="Email a verification code")
def otp_send(token: str, request: Request, db: Session = Depends(get_db)):
    rec, env = find(token, db)
    if not otp_required(db, env, rec):
        raise HTTPException(409, "Verification is not required for this link.")
    now = utc_now()
    recent = db.scalars(select(RecipientSession).where(RecipientSession.recipient_id == rec.id, RecipientSession.created_at > now - timedelta(hours=1))
                        .order_by(RecipientSession.created_at.desc())).all()
    if len(recent) >= 5:
        raise HTTPException(429, "Too many codes requested. Please try again later.")
    if recent and (now - aware(recent[0].created_at)).total_seconds() < 30:
        raise HTTPException(429, "Please wait 30 seconds before requesting another code.")
    code = f"{secrets.randbelow(10 ** 6):06d}"
    db.add(RecipientSession(recipient_id=rec.id, code_hash=_code_hash(rec.id, code), code_expires_at=now + timedelta(minutes=settings.otp_minutes)))
    cfg, owner = mailer.resolve(db, env.user_id), db.get(User, env.user_id)
    subject, text, html = mailer.otp_email(sender=owner.name or owner.email, filename=env.name, code=code, minutes=settings.otp_minutes)
    try:
        mailer.send_one(cfg, mailer.build_message(cfg, rec.email, subject, text, html, reply_to=owner.email))
    except mailer.MailError as e:
        logger.warning("OTP email for recipient %s failed: %s", rec.id, e)
        db.rollback()
        raise HTTPException(502, f"We couldn't send the code right now. Please try again, or contact {owner.name or owner.email}.")
    svc.add_event(db, env, "otp_sent", f"to={_mask(rec.email)}; ip={request.client.host if request.client else '-'}", rec.id)
    db.commit()
    return {"sent": True, "email_hint": _mask(rec.email), "expires_minutes": settings.otp_minutes}


class OtpIn(BaseModel):
    code: str


@router.post("/{token}/otp/verify", summary="Check the emailed code")
def otp_verify(token: str, body: OtpIn, request: Request, db: Session = Depends(get_db)):
    rec, env = find(token, db)
    s = db.scalar(select(RecipientSession).where(RecipientSession.recipient_id == rec.id).order_by(RecipientSession.created_at.desc()).limit(1))
    if s is None or s.verified_at:
        raise HTTPException(409, "Request a verification code first.")
    if aware(s.code_expires_at) <= utc_now():
        raise HTTPException(410, "This code has expired. Request a new one.")
    if s.attempts >= settings.otp_max_attempts:
        raise HTTPException(429, "Too many incorrect attempts. Request a new code.")
    s.attempts += 1
    ip = request.client.host if request.client else "-"
    if not hmac.compare_digest(s.code_hash, _code_hash(rec.id, body.code.strip().replace(" ", ""))):
        svc.add_event(db, env, "otp_failed", f"ip={ip}", rec.id)
        db.commit()
        raise HTTPException(400, "That code is incorrect.")
    access = secrets.token_urlsafe(32)
    s.access_hash, s.verified_at = svc.hash_token(access), utc_now()
    svc.add_event(db, env, "otp_verified", f"ip={ip}", rec.id)
    db.commit()
    return {"verify_token": access}


@router.get("/{token}/pages/{page}.png", summary="A page as this recipient sees it")
def page_image(token: str, page: int, v: str | None = Query(None), x_verify: str | None = Header(None), db: Session = Depends(get_db)):
    rec, env = find(token, db)
    _need_verified(db, env, rec, _vt(v, x_verify))
    hide = {f.id for f in _editable(db, rec)} if env.status == "in_progress" and rec.status in ("pending", "viewed") else set()
    try:
        png = svc.render_png(db, env, page, hide)
    except svc.EnvelopeError as e:
        raise HTTPException(e.status, e.message)
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/{token}/document", summary="Download the document (the final PDF once completed)")
def download(token: str, v: str | None = Query(None), x_verify: str | None = Header(None), db: Session = Depends(get_db)):
    rec, env = find(token, db)
    _need_verified(db, env, rec, _vt(v, x_verify))
    if env.final_document_id:
        doc = db.get(Document, env.final_document_id)
        return FileResponse(resolve(doc.file_path), media_type="application/pdf", filename=doc.filename)
    import pymupdf
    with pymupdf.open(svc._source_path(db, env)) as pdf:
        draw_fields(pdf, [f for f in svc.env_fields(db, env.id) if f.value or f.sig_path], font_resolver(db, env.user_id, None),
                    svc._image_loader(), FillReport())
        data = finish(pdf)
    return Response(data, media_type="application/pdf")


@router.post("/{token}/submit", summary="Finish signing")
def submit(
    token: str, request: Request,
    values: str = Form("{}", description="JSON {field name: value}"),
    consent: bool = Form(False),
    signature: UploadFile | None = File(default=None), initials: UploadFile | None = File(default=None),
    v: str | None = Query(None), x_verify: str | None = Header(None), db: Session = Depends(get_db),
):
    rec, env = find(token, db)
    _need_verified(db, env, rec, _vt(v, x_verify))
    try:
        data = json.loads(values or "{}")
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(422, "values must be a JSON object")
    try:
        svc.submit(db, env, rec, data, read_upload(signature, limit_mb=1) if signature else None,
                   read_upload(initials, limit_mb=1) if initials else None, consent, request.client.host if request.client else "-")
        db.commit()
    except svc.EnvelopeError as e:
        db.rollback()
        raise HTTPException(e.status, {"message": e.message, "errors": e.errors} if e.errors else e.message)
    return {"status": env.status, "recipient_status": rec.status, "completed": env.status == "completed", "return_url": env.return_url}


class DeclineIn(BaseModel):
    reason: str = ""


@router.post("/{token}/decline", summary="Decline to sign")
def decline(token: str, body: DeclineIn, request: Request, v: str | None = Query(None), x_verify: str | None = Header(None),
            db: Session = Depends(get_db)):
    rec, env = find(token, db)
    _need_verified(db, env, rec, _vt(v, x_verify))
    try:
        svc.decline(db, env, rec, body.reason.strip(), request.client.host if request.client else "-")
        db.commit()
    except svc.EnvelopeError as e:
        db.rollback()
        raise HTTPException(e.status, e.message)
    return {"status": env.status}
