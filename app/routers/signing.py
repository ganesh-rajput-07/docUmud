import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import (
    BulkItem, Document, EmailOutbox, SignatureEvent, SignatureRequest, SignerSession, User, aware, new_id, utc_now,
)
from app.routers.documents import verified_path
from app.security import get_current_user
from app.services.access import get_owned_document, read_upload
from app.services import mailer
from app.services.certificate import build_certificate
from app.services.outbox import enqueue, signing_link
from app.services.signing_service import (
    add_event,
    apply_signature,
    hash_token,
    render_page,
    token_for,
    verify_chain,
)
from app.services.webhook_service import emit
from app.services.storage import InvalidDocument, hash_file, new_path, register_pdf

router = APIRouter(prefix="/signing", tags=["Signing"])
logger = logging.getLogger("documud.signing")


class RequestIn(BaseModel):
    document_id: str
    mode: str  # "email" | "in_person"
    recipient_name: str = ""
    recipient_email: EmailStr | None = None


def _link(token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/sign/{token}"


def _out(r: SignatureRequest, filename: str) -> dict:
    return {
        "id": r.id,
        "document_id": r.document_id,
        "filename": filename,
        "mode": r.mode,
        "recipient_name": r.recipient_name,
        "recipient_email": r.recipient_email,
        "status": r.status,
        "signed_document_id": r.signed_document_id,
        "expires_at": aware(r.expires_at).isoformat(),
        "signed_at": aware(r.signed_at).isoformat() if r.signed_at else None,
        "created_at": aware(r.created_at).isoformat(),
    }


# ---------- sender (authenticated) ----------

@router.post("/requests", status_code=201)
def create_request(
    body: RequestIn, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    if body.mode not in ("email", "in_person"):
        raise HTTPException(422, "mode must be 'email' or 'in_person'")
    if body.mode == "email" and not body.recipient_email:
        raise HTTPException(422, "Recipient email is required for email signing.")
    doc = get_owned_document(db, body.document_id, user.id)
    source = verified_path(doc)

    days = 1 if body.mode == "in_person" else settings.signing_link_days
    req_id = new_id()
    token = token_for(req_id)
    req = SignatureRequest(
        id=req_id,
        document_id=doc.id,
        sender_id=user.id,
        mode=body.mode,
        recipient_name=body.recipient_name.strip()[:120],
        recipient_email=(body.recipient_email or "").lower(),
        token_hash=hash_token(token),
        document_hash=hash_file(source),
        expires_at=datetime.now(timezone.utc) + timedelta(days=days),
    )
    db.add(req)
    db.flush()
    add_event(db, req, "created", f"mode={req.mode}; recipient={req.recipient_email or req.recipient_name}; ip={request.client.host if request.client else '-'}")

    link = _link(token)
    cfg = mailer.resolve(db, user.id)
    emailed, email_error = False, None
    if body.mode == "email":
        row = enqueue(db, req, "invite", req.recipient_email)   # records the outcome; sent right now below
        if cfg:
            subject, text, html = mailer.invite_email(
                sender=user.name or user.email, filename=doc.filename, link=link, days=days, recipient=req.recipient_name)
            try:
                mailer.send_one(cfg, mailer.build_message(cfg, req.recipient_email, subject, text, html, reply_to=user.email))
                row.status, row.sent_at, row.attempts, emailed = "sent", utc_now(), 1, True
            except mailer.MailError as e:
                row.status, row.last_error, row.attempts, email_error = "failed", str(e), 1, str(e)
        add_event(db, req, "email_sent" if emailed else "email_not_sent", req.recipient_email)
    db.commit()
    return {
        **_out(req, doc.filename),
        "signing_link": link,
        "email_sent": emailed,
        "email_configured": cfg is not None,
        "email_error": email_error,
    }


@router.get("/requests")
def list_requests(
    include_bulk: bool = False, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Your signature requests. Requests created by a bulk send are listed under /bulk instead."""
    query = (
        select(SignatureRequest, Document.filename)
        .join(Document, Document.id == SignatureRequest.document_id)
        .where(SignatureRequest.sender_id == user.id)
        .order_by(SignatureRequest.created_at.desc())
    )
    if not include_bulk:
        query = query.where(SignatureRequest.id.not_in(select(BulkItem.request_id)))
    rows = db.execute(query).all()
    now = datetime.now(timezone.utc)
    out = []
    for r, filename in rows:
        if r.status == "pending" and aware(r.expires_at) <= now:
            r.status = "expired"
        out.append(_out(r, filename))
    db.commit()
    return out


def _owned_request(db: Session, request_id: str, user: User) -> SignatureRequest:
    req = db.get(SignatureRequest, request_id)
    if not req or req.sender_id != user.id:
        raise HTTPException(404, "Signature request not found")
    return req


@router.post("/requests/{request_id}/cancel")
def cancel_request(
    request_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    req = _owned_request(db, request_id, user)
    claimed = db.execute(
        update(SignatureRequest)
        .where(SignatureRequest.id == req.id, SignatureRequest.status == "pending")
        .values(status="cancelled")
    )
    if claimed.rowcount != 1:
        raise HTTPException(409, f"Cannot cancel a request that is {req.status}.")
    add_event(db, req, "cancelled", f"by={user.email}")
    emit(db, user.id, "signature.cancelled", {"request_id": req.id, "document_id": req.document_id})
    db.commit()
    return {"status": "cancelled"}


@router.post("/requests/{request_id}/remind")
def remind_request(
    request_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Email the signer their link again."""
    req = _owned_request(db, request_id, user)
    if req.status != "pending" or req.mode != "email" or not req.recipient_email:
        raise HTTPException(409, "Only pending email requests can be reminded.")
    if hash_token(token_for(req.id)) != req.token_hash:
        raise HTTPException(409, "This request predates reminders; cancel it and send a new one.")
    if mailer.resolve(db, user.id) is None:
        raise HTTPException(409, "Set up your email in Settings first.")
    recent = db.scalar(select(func.count()).select_from(EmailOutbox).where(
        EmailOutbox.request_id == req.id, EmailOutbox.kind.in_(("invite", "reminder")),
        EmailOutbox.created_at > utc_now() - timedelta(hours=1)))
    if recent:
        raise HTTPException(429, "The signer was emailed within the last hour. Please wait before reminding again.")
    enqueue(db, req, "reminder", req.recipient_email)
    add_event(db, req, "reminder_queued", f"by={user.email}")
    db.commit()
    return {"queued": True}


@router.get("/requests/{request_id}/certificate")
def request_certificate(
    request_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """A completion certificate (PDF) summarising who signed, when, and the audit trail."""
    req = _owned_request(db, request_id, user)
    if req.status != "signed":
        raise HTTPException(409, "A certificate is available once the document is signed.")
    events = db.scalars(select(SignatureEvent).where(SignatureEvent.request_id == req.id).order_by(SignatureEvent.created_at)).all()
    sender = db.get(User, req.sender_id)
    doc = db.get(Document, req.document_id)
    pdf = build_certificate(
        req=req, document_name=doc.filename, sender=f"{sender.name or sender.email} <{sender.email}>",
        events=events, chain_valid=verify_chain(events, req.id),
    )
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="certificate-{req.id[:8]}.pdf"'})


@router.get("/requests/{request_id}/events")
def request_events(
    request_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    req = _owned_request(db, request_id, user)
    events = db.scalars(
        select(SignatureEvent)
        .where(SignatureEvent.request_id == req.id)
        .order_by(SignatureEvent.created_at)
    ).all()
    return {
        "chain_valid": verify_chain(events, req.id),
        "document_hash": req.document_hash,
        "signed_file_hash": req.signed_file_hash,
        "events": [
            {"type": e.event_type, "details": e.details, "at": aware(e.created_at).isoformat()}
            for e in events
        ],
    }


# ---------- recipient (token = capability) ----------

def find_request(token: str, db: Session) -> SignatureRequest:
    req = db.scalar(
        select(SignatureRequest).where(SignatureRequest.token_hash == hash_token(token))
    )
    if not req:
        raise HTTPException(404, "Signing link not found")
    if req.status == "pending" and aware(req.expires_at) <= datetime.now(timezone.utc):
        req.status = "expired"
        add_event(db, req, "expired")
        db.commit()
    if req.status == "expired":
        raise HTTPException(410, "This signing link has expired")
    if req.status == "cancelled":
        raise HTTPException(410, "This signing request was cancelled")
    if req.status == "signed":
        raise HTTPException(409, "This document has already been signed")
    if req.status != "pending":
        raise HTTPException(409, "This signing request is no longer available")
    return req


def _pending_source(req: SignatureRequest, db: Session) -> Path:
    doc = db.get(Document, req.document_id)
    path = verified_path(doc)
    if hash_file(path) != req.document_hash:
        raise HTTPException(409, "Document changed since the request was created")
    return path


def otp_required(db: Session, req: SignatureRequest) -> bool:
    """Email signers must enter an emailed code first, whenever the sender's mailbox can deliver it."""
    return (settings.require_signer_otp and req.mode == "email" and bool(req.recipient_email)
            and mailer.resolve(db, req.sender_id) is not None)


def _code_hash(request_id: str, code: str) -> str:
    return hmac.new(settings.secret_key.encode(), f"otp:{request_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:1]}{'*' * max(len(name) - 1, 1)}@{domain}"


def require_verified(db: Session, req: SignatureRequest, token: str | None) -> None:
    if not otp_required(db, req):
        return
    ok = token and db.scalar(select(SignerSession.id).where(
        SignerSession.request_id == req.id, SignerSession.access_hash == hash_token(token),
        SignerSession.verified_at.is_not(None)))
    if not ok:
        raise HTTPException(403, "Please verify your email address to continue.")


def _vt(v: str | None, x_verify: str | None) -> str | None:
    return x_verify or v


@router.get("/public/{token}/info")
def public_info(token: str, db: Session = Depends(get_db)):
    req = find_request(token, db)
    doc = db.get(Document, req.document_id)
    sender = db.get(User, req.sender_id)
    needs_code = otp_required(db, req)
    return {
        "filename": doc.filename,
        "pages": doc.pages,
        "sender": sender.name or sender.email,
        "recipient_name": req.recipient_name,
        "recipient_email": req.recipient_email,
        "mode": req.mode,
        "otp_required": needs_code,
        "email_hint": _mask(req.recipient_email) if needs_code else "",
    }


@router.post("/public/{token}/otp/send")
def otp_send(token: str, request: Request, db: Session = Depends(get_db)):
    req = find_request(token, db)
    if not otp_required(db, req):
        raise HTTPException(409, "Verification is not required for this request.")
    now = utc_now()
    sessions = db.scalars(select(SignerSession).where(
        SignerSession.request_id == req.id, SignerSession.created_at > now - timedelta(hours=1)
    ).order_by(SignerSession.created_at.desc())).all()
    if len(sessions) >= 5:
        raise HTTPException(429, "Too many codes requested. Please try again later.")
    if sessions and (now - aware(sessions[0].created_at)).total_seconds() < 30:
        raise HTTPException(429, "Please wait 30 seconds before requesting another code.")
    code = f"{secrets.randbelow(10 ** 6):06d}"
    db.add(SignerSession(request_id=req.id, code_hash=_code_hash(req.id, code),
                         code_expires_at=now + timedelta(minutes=settings.otp_minutes)))
    cfg = mailer.resolve(db, req.sender_id)
    sender = db.get(User, req.sender_id)
    doc = db.get(Document, req.document_id)
    subject, text, html = mailer.otp_email(sender=sender.name or sender.email, filename=doc.filename,
                                           code=code, minutes=settings.otp_minutes)
    try:
        mailer.send_one(cfg, mailer.build_message(cfg, req.recipient_email, subject, text, html, reply_to=sender.email))
    except mailer.MailError as e:
        logger.warning("OTP email for request %s failed: %s", req.id, e)
        db.rollback()
        raise HTTPException(502, f"We couldn't send the code right now. Please try again, or contact {sender.name or sender.email}.")
    add_event(db, req, "otp_sent", f"to={_mask(req.recipient_email)}; ip={request.client.host if request.client else '-'}")
    db.commit()
    return {"sent": True, "email_hint": _mask(req.recipient_email), "expires_minutes": settings.otp_minutes}


class OtpIn(BaseModel):
    code: str


@router.post("/public/{token}/otp/verify")
def otp_verify(token: str, body: OtpIn, request: Request, db: Session = Depends(get_db)):
    req = find_request(token, db)
    session = db.scalar(select(SignerSession).where(SignerSession.request_id == req.id)
                        .order_by(SignerSession.created_at.desc()).limit(1))
    if session is None or session.verified_at:
        raise HTTPException(409, "Request a verification code first.")
    if aware(session.code_expires_at) <= utc_now():
        raise HTTPException(410, "This code has expired. Request a new one.")
    if session.attempts >= settings.otp_max_attempts:
        raise HTTPException(429, "Too many incorrect attempts. Request a new code.")
    session.attempts += 1
    ip = request.client.host if request.client else "-"
    if not hmac.compare_digest(session.code_hash, _code_hash(req.id, body.code.strip().replace(" ", ""))):
        add_event(db, req, "otp_failed", f"ip={ip}")
        db.commit()
        raise HTTPException(400, "That code is incorrect.")
    access = secrets.token_urlsafe(32)
    session.access_hash, session.verified_at = hash_token(access), utc_now()
    add_event(db, req, "otp_verified", f"ip={ip}")
    db.commit()
    return {"verify_token": access}


@router.get("/public/{token}/document")
def public_document(token: str, v: str | None = Query(None), x_verify: str | None = Header(None),
                    db: Session = Depends(get_db)):
    req = find_request(token, db)
    require_verified(db, req, _vt(v, x_verify))
    return FileResponse(_pending_source(req, db), media_type="application/pdf")


@router.get("/public/{token}/pages/{page}.png")
def public_page(token: str, page: int, v: str | None = Query(None), x_verify: str | None = Header(None),
                db: Session = Depends(get_db)):
    req = find_request(token, db)
    require_verified(db, req, _vt(v, x_verify))
    try:
        png = render_page(_pending_source(req, db), page)
    except InvalidDocument as e:
        raise HTTPException(404, str(e))
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/public/{token}/viewed")
def mark_viewed(token: str, request: Request, v: str | None = Query(None), x_verify: str | None = Header(None),
                db: Session = Depends(get_db)):
    req = find_request(token, db)
    require_verified(db, req, _vt(v, x_verify))
    add_event(db, req, "viewed", f"ip={request.client.host if request.client else '-'}")
    db.commit()
    return {"ok": True}


@router.post("/public/{token}/sign")
def public_sign(
    token: str,
    request: Request,
    signature: UploadFile = File(...),
    signer_name: str = Form(...),
    page_number: int = Form(0),
    x: float = Form(...),
    y: float = Form(...),
    width: float = Form(...),
    consent: bool = Form(False),
    v: str | None = Query(None),
    x_verify: str | None = Header(None),
    db: Session = Depends(get_db),
):
    if not consent:
        raise HTTPException(422, "Consent is required to sign electronically.")
    signer_name = signer_name.strip()[:120]
    if not signer_name:
        raise HTTPException(422, "Please type your full name.")
    req = find_request(token, db)
    require_verified(db, req, _vt(v, x_verify))
    verified_by_code = otp_required(db, req)
    in_batch = db.scalar(select(BulkItem.id).where(BulkItem.request_id == req.id)) is not None
    png = read_upload(signature, limit_mb=1)
    source = _pending_source(req, db)

    # One-time claim: only one concurrent request can move pending -> signing.
    claimed = db.execute(
        update(SignatureRequest)
        .where(SignatureRequest.id == req.id, SignatureRequest.status == "pending")
        .values(status="signing")
    )
    db.commit()
    if claimed.rowcount != 1:
        raise HTTPException(409, "This signing request is no longer available")

    ip = request.client.host if request.client else "-"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out, rel = new_path("results")
    try:
        apply_signature(
            source, out, signature_png=png, page_number=page_number, x=x, y=y,
            width=width, label=f"Signed by {signer_name} · {stamp}",
        )
        parent = db.get(Document, req.document_id)
        signed = register_pdf(
            db, user_id=req.sender_id, path=out, relative=rel,
            filename=f"{Path(parent.filename).stem}-signed.pdf", source="batch-signed" if in_batch else "signed",
            parent_id=parent.id,
        )
    except InvalidDocument as e:
        out.unlink(missing_ok=True)
        db.rollback()
        db.execute(
            update(SignatureRequest)
            .where(SignatureRequest.id == req.id).values(status="pending")
        )
        db.commit()
        raise HTTPException(422, str(e))
    except Exception:
        out.unlink(missing_ok=True)
        db.rollback()
        db.execute(
            update(SignatureRequest)
            .where(SignatureRequest.id == req.id).values(status="pending")
        )
        db.commit()
        raise

    req.status = "signed"
    req.signed_at = datetime.now(timezone.utc)
    req.signed_document_id = signed.id
    req.signed_file_hash = signed.file_hash
    req.recipient_name = signer_name
    add_event(db, req, "signed", f"name={signer_name}; ip={ip}; consent=true; identity={'email_code' if verified_by_code else 'link_only'}; signed_hash={signed.file_hash}")
    if mailer.resolve(db, req.sender_id) is not None:    # sender gets a notification, signer gets their copy
        sender = db.get(User, req.sender_id)
        enqueue(db, req, "signed_sender", sender.email)
        if req.recipient_email:
            enqueue(db, req, "signed_signer", req.recipient_email)
    emit(db, req.sender_id, "signature.signed", {
        "request_id": req.id, "document_id": req.document_id, "signed_document_id": signed.id,
        "signer_name": signer_name, "recipient_email": req.recipient_email,
        "mode": req.mode, "signed_at": req.signed_at.isoformat(), "signed_file_hash": signed.file_hash,
    })
    db.commit()
    return {"status": "signed", "signed_file_hash": signed.file_hash}
