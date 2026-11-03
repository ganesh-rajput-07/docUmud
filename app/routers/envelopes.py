import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import (
    Document, Envelope, EnvelopeBatch, EnvelopeEvent, EnvelopeField, EnvelopeMail, EnvelopeRecipient, IdempotencyKey, SignatureProfile,
    Template, User, aware, utc_now,
)
from app.security import get_current_user
from app.services import data_normalizer as dn
from app.services import envelope_service as svc
from app.services import mailer
from app.services.certificate import build_envelope_certificate
from app.services.envelope_mail import enqueue_mail
from app.services.field_types import FieldError
from app.services.fill_service import FillReport
from app.services.render import draw_fields, finish
from app.services.signature_images import typed_png, validate_png
from app.services.storage import InvalidDocument, resolve
from app.services.template_service import font_resolver, jl

router = APIRouter(prefix="/v1", tags=["Envelopes"])


class RecipientIn(BaseModel):
    role: str = Field(description="A role from the template (e.g. 'client'), or any name with kind 'cc'")
    name: str = ""
    email: str = ""
    kind: Literal["signer", "cc"] | None = None
    deliver: Literal["email", "none"] = Field(default="email", description="'none' = embedded signing: you show the signing_url yourself")
    verify: bool = Field(default=True, description="Require an emailed one-time code before this person can open the document")
    order: int | None = Field(default=None, description="Override the role's signing order")


class EnvelopeIn(BaseModel):
    template_id: str
    name: str | None = Field(default=None, max_length=200, description="Defaults to the template name")
    message: str = Field(default="", max_length=2000, description="Shown in the invitation email")
    recipients: list[RecipientIn] = []
    data: Any = Field(default=None, description="Field values in any shape: flat, nested, or [{name, value}] pairs. See docs/API.md")
    field_map: dict[str, str] | None = Field(default=None, description="Explicit {field name: path in data}, e.g. {'client_name': 'customer.full_name'}")
    signing_order: Literal["sequential", "parallel"] = "sequential"
    expires_in_days: int = Field(default=14, ge=1, le=90)
    external_id: str | None = Field(default=None, max_length=120, description="Your own reference; unique per account, searchable")
    metadata: dict[str, Any] = Field(default={}, description="Echoed back on reads and in webhooks")
    return_url: str | None = Field(default=None, max_length=2000, description="Where to send signers after they finish")
    send: bool = Field(default=True, description="false = create a draft; call POST /v1/envelopes/{id}/send later")
    office_auto_sign: bool = Field(default=False, description="Apply the office's saved signature (PUT /v1/signature-profile) to office-role signature fields")
    strict: bool = Field(default=False, description="Fail if any required office-role data field is missing")


def err(e: svc.EnvelopeError) -> HTTPException:
    detail = {"message": e.message, "errors": e.errors} if e.errors else e.message
    return HTTPException(e.status, detail)


def get_owned_envelope(db: Session, envelope_id: str, user: User) -> Envelope:
    env = db.get(Envelope, envelope_id)
    if not env or env.user_id != user.id:
        raise HTTPException(404, "Envelope not found")
    return env


def _check_return_url(url: str | None) -> None:
    if not url:
        return
    p = urlparse(url)
    ok = p.scheme == "https" or (p.scheme == "http" and settings.environment == "development")
    if not ok or not p.netloc:
        raise HTTPException(422, "return_url must be an https:// URL.")


def _hash_body(body: EnvelopeIn) -> str:
    return hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True, default=str).encode()).hexdigest()


# ---------------- create ----------------

@router.post("/envelopes", status_code=201, summary="Create an envelope from a template, data and recipients")
def create_envelope(
    body: EnvelopeIn, response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200,
                                         description="Retrying with the same key returns the original envelope instead of creating a duplicate"),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Fills the template with `data`, creates the recipients, and (unless `send` is false) starts the signing.
    Every recipient comes back with a `signing_url` you can email yourself or embed in your own app."""
    _check_return_url(body.return_url)
    digest = _hash_body(body)
    if idempotency_key:
        prior = db.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == user.id, IdempotencyKey.key == idempotency_key))
        if prior:
            if prior.request_hash != digest:
                raise HTTPException(422, "This Idempotency-Key was already used with a different request body.")
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
            return svc.envelope_out(db, db.get(Envelope, prior.envelope_id))
    template = db.get(Template, body.template_id)
    if not template or template.user_id != user.id:
        raise HTTPException(404, "Template not found")
    if template.status != "active":
        raise HTTPException(409, "This template is archived.")
    if body.external_id:
        clash = db.scalar(select(Envelope.id).where(Envelope.user_id == user.id, Envelope.external_id == body.external_id))
        if clash:
            raise HTTPException(409, {"message": f"external_id '{body.external_id}' is already used", "errors": {"external_id": f"envelope {clash}"}})
    try:
        env, warnings = svc.create_envelope(
            db, user, template, recipients=[r.model_dump() for r in body.recipients], data=body.data, field_map=body.field_map,
            name=body.name, message=body.message, signing_order=body.signing_order, expires_in_days=body.expires_in_days,
            external_id=body.external_id, metadata=body.metadata, return_url=body.return_url, send=body.send,
            office_auto_sign=body.office_auto_sign, strict=body.strict)
    except svc.EnvelopeError as e:
        db.rollback()
        raise err(e)
    if idempotency_key:
        db.add(IdempotencyKey(user_id=user.id, key=idempotency_key, request_hash=digest, envelope_id=env.id))
    db.commit()
    return svc.envelope_out(db, env, warnings=warnings)


# ---------------- read ----------------

def _cursor(ts: datetime, id_: str) -> str:
    return base64.urlsafe_b64encode(f"{aware(ts).isoformat()}|{id_}".encode()).decode()


def _parse_cursor(raw: str) -> tuple[datetime, str]:
    try:
        ts, id_ = base64.urlsafe_b64decode(raw.encode()).decode().split("|", 1)
        return datetime.fromisoformat(ts), id_
    except Exception:
        raise HTTPException(422, "Invalid cursor.")


@router.get("/envelopes", summary="List envelopes (cursor-paginated, filterable)")
def list_envelopes(
    status: str | None = Query(None, description="draft | in_progress | completed | declined | cancelled | expired (comma-separate several)"),
    template_id: str | None = None, external_id: str | None = Query(None, description="Exact match on your own reference (indexed: use this to look an envelope up)"),
    q: str | None = Query(None, description="Substring search of name, external_id or a recipient's email (scans, so prefer external_id for lookups)"),
    created_after: datetime | None = None, created_before: datetime | None = None,
    limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Newest first. Pass `next_cursor` back as `cursor` for the next page; this stays fast however many envelopes you have."""
    query = select(Envelope).where(Envelope.user_id == user.id)
    if status:
        query = query.where(Envelope.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    if template_id:
        query = query.where(Envelope.template_id == template_id)
    if external_id:
        query = query.where(Envelope.external_id == external_id)
    if created_after:
        query = query.where(Envelope.created_at >= created_after)
    if created_before:
        query = query.where(Envelope.created_at < created_before)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(Envelope.name.ilike(like), Envelope.external_id.ilike(like),
                                Envelope.id.in_(select(EnvelopeRecipient.envelope_id).where(EnvelopeRecipient.email.ilike(like)))))
    if cursor:
        ts, id_ = _parse_cursor(cursor)
        query = query.where(or_(Envelope.created_at < ts, (Envelope.created_at == ts) & (Envelope.id < id_)))
    rows = db.scalars(query.order_by(Envelope.created_at.desc(), Envelope.id.desc()).limit(limit + 1)).all()
    page = rows[:limit]
    recs: dict[str, list] = {}
    if page:
        for r in db.scalars(select(EnvelopeRecipient).where(EnvelopeRecipient.envelope_id.in_([e.id for e in page])).order_by(EnvelopeRecipient.order)):
            recs.setdefault(r.envelope_id, []).append(r)
    data = []
    for e in page:
        rs = [r for r in recs.get(e.id, []) if r.kind != "cc"]
        data.append({"id": e.id, "name": e.name, "status": e.status, "template_id": e.template_id, "external_id": e.external_id,
                     "created_at": aware(e.created_at).isoformat(), "completed_at": aware(e.completed_at).isoformat() if e.completed_at else None,
                     "expires_at": aware(e.expires_at).isoformat(), "signed": sum(r.status == "signed" for r in rs), "total": len(rs),
                     "recipients": [{"role": r.role, "name": r.name, "email": r.email, "status": r.status} for r in rs]})
    return {"data": data, "has_more": len(rows) > limit, "next_cursor": _cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None}


@router.get("/envelopes/stats", summary="Counts by status")
def stats(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    counts = dict(db.execute(select(Envelope.status, func.count()).where(Envelope.user_id == user.id).group_by(Envelope.status)).all())
    return {"total": sum(counts.values()), **{s: counts.get(s, 0) for s in ("draft", "in_progress", "completed", "declined", "cancelled", "expired")}}


@router.get("/envelopes/{envelope_id}", summary="Get an envelope with recipients and field values")
def get_envelope(envelope_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    env = get_owned_envelope(db, envelope_id, user)
    out = svc.envelope_out(db, env, with_fields=True)
    mail = db.scalars(select(EnvelopeMail).where(EnvelopeMail.envelope_id == env.id).order_by(EnvelopeMail.created_at)).all()
    out["emails"] = [{"kind": m.kind, "to": m.to_email, "status": m.status, "error": m.last_error} for m in mail]
    return out


@router.get("/envelopes/{envelope_id}/events", summary="Audit trail (hash-chained)")
def events(envelope_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    env = get_owned_envelope(db, envelope_id, user)
    evs = db.scalars(select(EnvelopeEvent).where(EnvelopeEvent.envelope_id == env.id).order_by(EnvelopeEvent.created_at)).all()
    return {"chain_valid": svc.verify_chain(list(evs), env.id), "final_sha256": env.final_hash,
            "events": [{"type": e.event_type, "details": e.details, "recipient_id": e.recipient_id, "at": aware(e.created_at).isoformat()} for e in evs]}


@router.get("/envelopes/{envelope_id}/document", summary="The completed PDF, or the current state while signing")
def document(envelope_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    env = get_owned_envelope(db, envelope_id, user)
    if env.final_document_id:
        doc = db.get(Document, env.final_document_id)
        return FileResponse(resolve(doc.file_path), media_type="application/pdf", filename=doc.filename)
    import pymupdf
    src = svc._source_path(db, env)
    with pymupdf.open(src) as pdf:
        draw_fields(pdf, [f for f in svc.env_fields(db, env.id) if f.value or f.sig_path], font_resolver(db, env.user_id, None),
                    svc._image_loader(), FillReport())
        data = finish(pdf)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{env.name}-in-progress.pdf"'})


@router.get("/envelopes/{envelope_id}/certificate", summary="Certificate of Completion (PDF)")
def certificate(envelope_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    env = get_owned_envelope(db, envelope_id, user)
    if env.status != "completed":
        raise HTTPException(409, "A certificate is available once the envelope is completed.")
    evs = list(db.scalars(select(EnvelopeEvent).where(EnvelopeEvent.envelope_id == env.id).order_by(EnvelopeEvent.created_at)).all())
    pdf = build_envelope_certificate(env=env, owner=f"{user.name or user.email} <{user.email}>", recipients=svc.env_recipients(db, env.id),
                                     events=evs, chain_valid=svc.verify_chain(evs, env.id))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="certificate-{env.id[:8]}.pdf"'})


# ---------------- actions ----------------

@router.post("/envelopes/{envelope_id}/send", summary="Send a draft envelope")
def send(envelope_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    env = get_owned_envelope(db, envelope_id, user)
    try:
        svc.send_envelope(db, env, user)
    except svc.EnvelopeError as e:
        raise err(e)
    db.commit()
    return svc.envelope_out(db, env)


@router.post("/envelopes/{envelope_id}/cancel", summary="Void an envelope")
def cancel(envelope_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    env = get_owned_envelope(db, envelope_id, user)
    try:
        svc.cancel(db, env, user.email)
    except svc.EnvelopeError as e:
        raise err(e)
    db.commit()
    return svc.envelope_out(db, env)


@router.post("/envelopes/{envelope_id}/remind", summary="Email waiting recipients again")
def remind(envelope_id: str, recipient_id: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    env = get_owned_envelope(db, envelope_id, user)
    if env.status != "in_progress":
        raise HTTPException(409, f"This envelope is {env.status}.")
    if mailer.resolve(db, user.id) is None:
        raise HTTPException(409, "Set up your email in Settings first.")
    queued = 0
    for r in svc.active_recipients(svc.env_recipients(db, env.id), env.signing_order):
        if (recipient_id and r.id != recipient_id) or not (r.deliver == "email" and r.email):
            continue
        recent = db.scalar(select(func.count()).select_from(EnvelopeMail).where(
            EnvelopeMail.recipient_id == r.id, EnvelopeMail.kind.in_(("invite", "reminder")),
            EnvelopeMail.created_at > utc_now() - timedelta(hours=1)))
        if recent:
            continue
        enqueue_mail(db, env, r, "reminder")
        svc.add_event(db, env, "reminder_queued", f"to={r.email}; by={user.email}", r.id)
        queued += 1
    db.commit()
    return {"queued": queued}


@router.patch("/envelopes/{envelope_id}/data", summary="Update field values on a draft envelope")
def update_data(envelope_id: str, body: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    env = get_owned_envelope(db, envelope_id, user)
    if env.status != "draft":
        raise HTTPException(409, "Data can only be changed while the envelope is a draft.")
    fields = svc.env_fields(db, env.id)
    try:
        raw, warnings = dn.resolve(body.get("data", body), fields, body.get("field_map") if "data" in body else None)
    except FieldError as e:
        raise HTTPException(422, str(e))
    dn.derive_names(raw, fields)
    clean, errors = dn.coerce_values(raw, fields)
    if errors:
        raise HTTPException(422, {"message": "Invalid values; nothing changed.", "errors": {f"data.{k}": v for k, v in errors.items()}})
    for f in fields:
        if f.name in clean and f.type not in svc.SIGN_TYPES | svc.AUTO_TYPES:
            f.value, f.locked = clean[f.name], bool(clean[f.name])
    env.rev += 1
    svc.add_event(db, env, "data_updated", f"fields={','.join(sorted(clean))}")
    db.commit()
    return svc.envelope_out(db, env, with_fields=True, warnings=warnings)


@router.post("/envelopes/{envelope_id}/recipients/{recipient_id}/signing-url", summary="Get the signing link for embedded signing")
def recipient_signing_url(envelope_id: str, recipient_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The link opens the signing page for that person. Use it in an iframe/redirect in your own app, or send it yourself."""
    env = get_owned_envelope(db, envelope_id, user)
    rec = db.get(EnvelopeRecipient, recipient_id)
    if not rec or rec.envelope_id != env.id or rec.kind == "cc":
        raise HTTPException(404, "Recipient not found")
    if env.status in svc.TERMINAL or rec.status in ("signed", "declined"):
        raise HTTPException(409, f"Nothing left to sign (envelope {env.status}, recipient {rec.status}).")
    svc.add_event(db, env, "signing_url_issued", f"role={rec.role}; by={user.email}", rec.id)
    db.commit()
    return {"url": svc.signing_url(rec.id), "recipient_id": rec.id, "status": rec.status, "expires_at": aware(env.expires_at).isoformat()}


# ---------------- the office's saved signature ----------------

def _profile(db: Session, user: User) -> SignatureProfile | None:
    return db.scalar(select(SignatureProfile).where(SignatureProfile.user_id == user.id))


@router.put("/signature-profile", summary="Save the office's signature and initials for automatic signing")
def put_signature_profile(
    signature: UploadFile | None = File(default=None, description="PNG with transparent background"),
    initials: UploadFile | None = File(default=None),
    typed_name: str = Form("", description="Alternatively: generate a typed signature from this name"),
    font: str = Form("GreatVibes", description="Bundled signature font, e.g. GreatVibes, Allura, Sacramento"),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    from app.services.access import read_upload

    sig = read_upload(signature, limit_mb=1) if signature else None
    ini = read_upload(initials, limit_mb=1) if initials else None
    if typed_name.strip():
        sig = sig or typed_png(typed_name.strip(), font)
        ini = ini or typed_png("".join(p[0] for p in typed_name.split() if p)[:4].upper() or typed_name[:2], font)
    if not sig:
        raise HTTPException(422, "Provide a signature PNG or typed_name.")
    try:
        validate_png(sig)
        if ini:
            validate_png(ini)
    except InvalidDocument as e:
        raise HTTPException(422, str(e))
    prof = _profile(db, user) or SignatureProfile(user_id=user.id, signature_path="")
    base = f"signatures/profiles/{user.id}"
    resolve(base + "-signature.png").parent.mkdir(parents=True, exist_ok=True)
    resolve(base + "-signature.png").write_bytes(sig)
    prof.signature_path = base + "-signature.png"
    if ini:
        resolve(base + "-initials.png").write_bytes(ini)
        prof.initials_path = base + "-initials.png"
    db.add(prof)
    db.commit()
    return {"has_signature": True, "has_initials": bool(prof.initials_path)}


@router.get("/signature-profile", summary="Do I have a saved signature?")
def get_signature_profile(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = _profile(db, user)
    return {"has_signature": bool(p and p.signature_path), "has_initials": bool(p and p.initials_path)}


@router.get("/signature-profile/{kind}.png", summary="Preview the saved signature or initials")
def profile_image(kind: Literal["signature", "initials"], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = _profile(db, user)
    rel = p and (p.initials_path if kind == "initials" else p.signature_path)
    if not rel or not resolve(rel).is_file():
        raise HTTPException(404, "Not saved")
    return FileResponse(resolve(rel), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.delete("/signature-profile", status_code=204, summary="Remove the saved signature")
def delete_signature_profile(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = _profile(db, user)
    if p:
        for rel in (p.signature_path, p.initials_path):
            if rel:
                resolve(rel).unlink(missing_ok=True)
        db.delete(p)
        db.commit()


# ---------------- batches (async, for volume) ----------------

class BatchItem(BaseModel):
    recipients: list[RecipientIn] = []
    data: Any = None
    field_map: dict[str, str] | None = None
    name: str | None = None
    external_id: str | None = Field(default=None, max_length=120)
    metadata: dict[str, Any] = {}


class BatchIn(BaseModel):
    template_id: str
    items: list[BatchItem] = Field(min_length=1)
    message: str = Field(default="", max_length=2000)
    signing_order: Literal["sequential", "parallel"] = "sequential"
    expires_in_days: int = Field(default=14, ge=1, le=90)
    send: bool = True
    office_auto_sign: bool = False
    strict: bool = False
    return_url: str | None = None


def _batch_out(b, errors: list | None = None) -> dict:
    return {"id": b.id, "status": b.status, "total": b.total, "created": b.created_count, "failed": b.failed_count,
            "progress": round((b.created_count + b.failed_count) / b.total, 3) if b.total else 1,
            "created_at": aware(b.created_at).isoformat(), "finished_at": aware(b.finished_at).isoformat() if b.finished_at else None,
            **({"errors": errors} if errors is not None else {}), "results_url": f"/v1/batches/{b.id}/results"}


@router.post("/envelopes/batch", status_code=202, summary="Create many envelopes in the background")
def create_batch(body: BatchIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Accepts up to `BATCH_MAX_ITEMS` items and returns immediately. The worker creates the envelopes in chunks (each item is
    independent: a bad item fails alone). Poll GET /v1/batches/{id} or subscribe to the `batch.completed` webhook."""
    template = db.get(Template, body.template_id)
    if not template or template.user_id != user.id:
        raise HTTPException(404, "Template not found")
    if template.status != "active":
        raise HTTPException(409, "This template is archived.")
    if len(body.items) > settings.batch_max_items:
        raise HTTPException(422, f"At most {settings.batch_max_items} items per batch.")
    _check_return_url(body.return_url)
    bid = str(uuid.uuid4())
    rel = f"batches/{bid}.json"
    resolve(rel).write_text(json.dumps([i.model_dump(exclude_none=True) for i in body.items], default=str), encoding="utf-8")
    opts = body.model_dump(include={"message", "signing_order", "expires_in_days", "send", "office_auto_sign", "strict", "return_url"})
    batch = EnvelopeBatch(id=bid, user_id=user.id, template_id=template.id, total=len(body.items), options=json.dumps(opts), payload_path=rel)
    db.add(batch)
    db.commit()
    return _batch_out(batch)


@router.get("/batches/{batch_id}", summary="Progress and the first errors of a batch")
def get_batch(batch_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.services.envelope_batch import results_path

    b = db.get(EnvelopeBatch, batch_id)
    if not b or b.user_id != user.id:
        raise HTTPException(404, "Batch not found")
    errors = []
    path = results_path(b)
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if "error" in row and len(errors) < 100:
                errors.append(row)
    return _batch_out(b, errors)


@router.get("/batches/{batch_id}/results", summary="One line per item: the envelope id, or the error (JSON Lines)")
def batch_results(batch_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.services.envelope_batch import results_path

    b = db.get(EnvelopeBatch, batch_id)
    if not b or b.user_id != user.id:
        raise HTTPException(404, "Batch not found")
    path = results_path(b)
    return Response(path.read_text(encoding="utf-8") if path.is_file() else "", media_type="application/x-ndjson")
