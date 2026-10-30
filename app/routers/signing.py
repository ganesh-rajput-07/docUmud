from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Document, SignatureEvent, SignatureRequest, User, aware
from app.routers.documents import verified_path
from app.security import get_current_user
from app.services.access import get_owned_document, read_upload
from app.services.email_service import email_enabled, send_signing_invite
from app.services.signing_service import (
    add_event,
    apply_signature,
    create_signing_token,
    hash_token,
    render_page,
    verify_chain,
)
from app.services.webhook_service import emit
from app.services.storage import InvalidDocument, hash_file, new_path, register_pdf

router = APIRouter(prefix="/signing", tags=["Signing"])


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
    token, token_hash = create_signing_token()
    req = SignatureRequest(
        document_id=doc.id,
        sender_id=user.id,
        mode=body.mode,
        recipient_name=body.recipient_name.strip()[:120],
        recipient_email=(body.recipient_email or "").lower(),
        token_hash=token_hash,
        document_hash=hash_file(source),
        expires_at=datetime.now(timezone.utc) + timedelta(days=days),
    )
    db.add(req)
    db.flush()
    add_event(db, req, "created", f"mode={req.mode}; recipient={req.recipient_email or req.recipient_name}; ip={request.client.host if request.client else '-'}")

    link = _link(token)
    emailed = False
    if body.mode == "email":
        emailed = send_signing_invite(
            req.recipient_email, user.name or user.email, doc.filename, link, days
        )
        add_event(db, req, "email_sent" if emailed else "email_not_sent", req.recipient_email)
    db.commit()
    return {
        **_out(req, doc.filename),
        "signing_link": link,  # shown once; only its hash is stored
        "email_sent": emailed,
        "email_configured": email_enabled(),
    }


@router.get("/requests")
def list_requests(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.execute(
        select(SignatureRequest, Document.filename)
        .join(Document, Document.id == SignatureRequest.document_id)
        .where(SignatureRequest.sender_id == user.id)
        .order_by(SignatureRequest.created_at.desc())
    ).all()
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


@router.get("/public/{token}/info")
def public_info(token: str, db: Session = Depends(get_db)):
    req = find_request(token, db)
    doc = db.get(Document, req.document_id)
    sender = db.get(User, req.sender_id)
    return {
        "filename": doc.filename,
        "pages": doc.pages,
        "sender": sender.name or sender.email,
        "recipient_name": req.recipient_name,
        "recipient_email": req.recipient_email,
        "mode": req.mode,
    }


@router.get("/public/{token}/document")
def public_document(token: str, db: Session = Depends(get_db)):
    req = find_request(token, db)
    return FileResponse(_pending_source(req, db), media_type="application/pdf")


@router.get("/public/{token}/pages/{page}.png")
def public_page(token: str, page: int, db: Session = Depends(get_db)):
    req = find_request(token, db)
    try:
        png = render_page(_pending_source(req, db), page)
    except InvalidDocument as e:
        raise HTTPException(404, str(e))
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/public/{token}/viewed")
def mark_viewed(token: str, request: Request, db: Session = Depends(get_db)):
    req = find_request(token, db)
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
    db: Session = Depends(get_db),
):
    if not consent:
        raise HTTPException(422, "Consent is required to sign electronically.")
    signer_name = signer_name.strip()[:120]
    if not signer_name:
        raise HTTPException(422, "Please type your full name.")
    req = find_request(token, db)
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
            filename=f"{Path(parent.filename).stem}-signed.pdf", source="signed",
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
    add_event(db, req, "signed", f"name={signer_name}; ip={ip}; consent=true; signed_hash={signed.file_hash}")
    emit(db, req.sender_id, "signature.signed", {
        "request_id": req.id, "document_id": req.document_id, "signed_document_id": signed.id,
        "signer_name": signer_name, "recipient_email": req.recipient_email,
        "mode": req.mode, "signed_at": req.signed_at.isoformat(), "signed_file_hash": signed.file_hash,
    })
    db.commit()
    return {"status": "signed", "signed_file_hash": signed.file_hash}
