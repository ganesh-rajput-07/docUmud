import csv
import io
import json
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import (
    BulkBatch, BulkItem, Dataset, Document, EmailOutbox, SignatureRequest, User, aware, new_id, utc_now,
)
from app.routers.datasets import get_owned_dataset, load_rows
from app.routers.documents import verified_path
from app.routers.fill import _effective_mapping, _row_values, _style, _tags, _template
from app.security import get_current_user
from app.services import mailer
from app.services.access import get_owned_document
from app.services.fill_service import FillReport, fill_document, finish, safe_stem
from app.services.outbox import enqueue, signing_link
from app.services.signing_service import add_event, hash_token, token_for
from app.services.storage import InvalidDocument, hash_file, new_path, register_pdf, resolve
from app.services.webhook_service import emit

router = APIRouter(prefix="/bulk", tags=["Bulk signing"])


class BulkIn(BaseModel):
    document_id: str
    dataset_id: str
    email_column: str = Field(max_length=200)
    name_column: str | None = Field(default=None, max_length=200)
    personalize: bool = Field(default=False, description="Fill each recipient's copy from the sheet using the document's saved template")
    name: str = Field(default="", max_length=200)
    subject: str = Field(default="", max_length=200, description="Placeholders: {name}, {document}")
    message: str = Field(default="", max_length=1000, description="Placeholders: {name}, {document}")
    expires_days: int = Field(default=7, ge=1, le=30)
    start: int = Field(default=1, ge=1)
    end: int | None = Field(default=None, ge=1)


@dataclass
class Plan:
    doc: Document
    ds: Dataset
    columns: list[str]
    valid: list[tuple[int, str, str, list[str]]]   # (row number, name, email, row)
    invalid: list[dict]
    duplicates: list[dict]


def _plan(db: Session, user: User, body: BulkIn) -> Plan:
    doc = get_owned_document(db, body.document_id, user.id)
    ds = get_owned_dataset(db, body.dataset_id, user.id)
    columns = json.loads(ds.columns)
    if body.email_column not in columns:
        raise HTTPException(422, f"Column '{body.email_column}' is not in the sheet.")
    if body.name_column and body.name_column not in columns:
        raise HTTPException(422, f"Column '{body.name_column}' is not in the sheet.")
    rows = load_rows(ds)
    end = min(body.end or len(rows), len(rows))
    if body.start > end:
        raise HTTPException(422, f"Row range is empty (the sheet has {len(rows)} rows).")
    ei = columns.index(body.email_column)
    ni = columns.index(body.name_column) if body.name_column else None
    valid, invalid, duplicates, seen = [], [], [], set()
    for n in range(body.start, end + 1):
        row = rows[n - 1]
        raw = row[ei].strip()
        try:
            email = validate_email(raw, check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            invalid.append({"row": n, "value": raw[:80], "reason": "Empty" if not raw else "Not a valid email address"})
            continue
        if email in seen:
            duplicates.append({"row": n, "value": email})
            continue
        seen.add(email)
        valid.append((n, row[ni].strip()[:120] if ni is not None else "", email, row))
    return Plan(doc, ds, columns, valid, invalid, duplicates)


def _check_size(plan: Plan) -> None:
    if len(plan.valid) > settings.fill_max_rows:
        raise HTTPException(422, f"At most {settings.fill_max_rows} recipients per bulk send. Choose a smaller row range.")


@router.post("/preview")
def preview(body: BulkIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Check a send before committing: how many valid recipients, and which rows would be skipped."""
    plan = _plan(db, user, body)
    cfg = mailer.resolve(db, user.id)
    return {
        "total_rows": len(plan.valid) + len(plan.invalid) + len(plan.duplicates),
        "valid": len(plan.valid),
        "invalid": plan.invalid[:50], "invalid_count": len(plan.invalid),
        "duplicates": plan.duplicates[:50], "duplicate_count": len(plan.duplicates),
        "sample": [{"row": n, "name": name, "email": email} for n, name, email, _ in plan.valid[:8]],
        "over_limit": len(plan.valid) > settings.fill_max_rows,
        "email_configured": cfg is not None,
        "email_source": cfg.source if cfg else "none",
        "daily_limit": cfg.daily_limit if cfg else 0,
        "has_tags": bool(_tags(db, plan.doc)),
    }


@router.post("", status_code=201)
def create_batch(body: BulkIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Create one signature request per valid row and queue the invitation emails."""
    plan = _plan(db, user, body)
    _check_size(plan)
    if not plan.valid:
        raise HTTPException(422, f"No valid email addresses found in column '{body.email_column}'.")
    doc, report = plan.doc, FillReport()
    shared_source = verified_path(doc)          # integrity-checked once, not per recipient
    shared_hash = doc.file_hash

    tags = style = mapping = None
    if body.personalize:
        tags = _tags(db, doc)
        if not tags:
            raise HTTPException(409, "Tag the fields on this document first, or turn off personalisation.")
        tmpl = _template(db, doc, user)
        labels = list(dict.fromkeys(t.label for t in tags))
        mapping = _effective_mapping(tmpl, labels, plan.columns)
        style = _style(db, tmpl, user, report)

    stem = doc.filename.removesuffix(".pdf")
    batch = BulkBatch(
        user_id=user.id, document_id=doc.id, dataset_id=plan.ds.id, personalize=body.personalize,
        name=(body.name.strip() or f"{stem} · {plan.ds.name}")[:200], subject=body.subject.strip(),
        message=body.message.strip(), expires_days=body.expires_days, total=len(plan.valid),
    )
    db.add(batch)
    db.flush()
    created_files = []
    try:
        expires = datetime.now(timezone.utc) + timedelta(days=body.expires_days)
        for n, name, email, row in plan.valid:
            target, doc_hash = doc, shared_hash
            if body.personalize:
                pdf = fill_document(shared_source, tags, _row_values(tags, mapping, plan.columns, row), style, report)
                out, rel = new_path("results")
                out.write_bytes(finish(pdf))
                pdf.close()
                created_files.append(out)
                target = register_pdf(db, user_id=user.id, path=out, relative=rel,
                                      filename=f"{stem}-{safe_stem(name or email, str(n))}.pdf", source="batch", parent_id=doc.id)
                doc_hash = target.file_hash
            req_id = new_id()
            req = SignatureRequest(
                id=req_id, document_id=target.id, sender_id=user.id, mode="email", recipient_name=name,
                recipient_email=email, token_hash=hash_token(token_for(req_id)), document_hash=doc_hash, expires_at=expires,
            )
            db.add(req)
            db.flush()
            add_event(db, req, "created", f"mode=email; bulk={batch.id}; row={n}; recipient={email}")
            db.add(BulkItem(batch_id=batch.id, request_id=req_id, document_id=target.id, row_index=n, name=name, email=email))
            enqueue(db, req, "invite", email, batch.id)
        db.commit()
    except InvalidDocument as e:
        db.rollback()
        [f.unlink(missing_ok=True) for f in created_files]
        raise HTTPException(422, str(e))
    except Exception:
        db.rollback()
        [f.unlink(missing_ok=True) for f in created_files]
        raise
    cfg = mailer.resolve(db, user.id)
    return {
        "id": batch.id, "total": batch.total, "skipped_invalid": len(plan.invalid),
        "skipped_duplicates": len(plan.duplicates), "email_configured": cfg is not None,
        "warnings": report.warnings, "overflow": report.overflow,
    }


# ---------------- tracking ----------------

def _owned(db: Session, batch_id: str, user: User) -> BulkBatch:
    batch = db.get(BulkBatch, batch_id)
    if not batch or batch.user_id != user.id:
        raise HTTPException(404, "Bulk send not found")
    return batch


def _status(req: SignatureRequest) -> str:
    if req.status == "pending" and aware(req.expires_at) <= utc_now():
        return "expired"
    return req.status


def _items(db: Session, batch: BulkBatch) -> list[dict]:
    rows = db.execute(
        select(BulkItem, SignatureRequest)
        .join(SignatureRequest, SignatureRequest.id == BulkItem.request_id)
        .where(BulkItem.batch_id == batch.id).order_by(BulkItem.row_index)
    ).all()
    mails: dict[str, list[EmailOutbox]] = defaultdict(list)
    for m in db.scalars(select(EmailOutbox).where(EmailOutbox.batch_id == batch.id).order_by(EmailOutbox.created_at)):
        mails[m.request_id].append(m)
    out = []
    for item, req in rows:
        history = mails.get(req.id, [])
        last = history[-1] if history else None
        out.append({
            "item_id": item.id, "request_id": req.id, "row": item.row_index, "name": item.name, "email": item.email,
            "status": _status(req), "document_id": item.document_id, "signed_document_id": req.signed_document_id,
            "email_status": last.status if last else "none", "email_error": last.last_error if last else None,
            "reminders": sum(1 for m in history if m.kind == "reminder"),
            "signed_at": aware(req.signed_at).isoformat() if req.signed_at else None,
            "signed_file_hash": req.signed_file_hash,
        })
    return out


def _counts(items: list[dict]) -> dict:
    c = defaultdict(int)
    for i in items:
        c[i["status"]] += 1
        c[f"email_{i['email_status']}"] += 1
    return {"total": len(items), "signed": c["signed"], "pending": c["pending"] + c["signing"],
            "cancelled": c["cancelled"], "expired": c["expired"], "email_sent": c["email_sent"],
            "email_failed": c["email_failed"], "email_pending": c["email_pending"],
            "email_not_configured": c["email_not_configured"]}


def _batch_out(batch: BulkBatch) -> dict:
    return {"id": batch.id, "name": batch.name, "document_id": batch.document_id, "personalize": batch.personalize,
            "subject": batch.subject, "message": batch.message, "expires_days": batch.expires_days,
            "created_at": aware(batch.created_at).isoformat()}


@router.get("")
def list_batches(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    batches = db.scalars(select(BulkBatch).where(BulkBatch.user_id == user.id).order_by(BulkBatch.created_at.desc()).limit(100)).all()
    out = []
    for b in batches:
        c = _counts(_items(db, b))
        out.append({**_batch_out(b), "total": c["total"], "signed": c["signed"], "pending": c["pending"],
                    "email_failed": c["email_failed"]})
    return out


@router.get("/{batch_id}")
def get_batch(batch_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    batch = _owned(db, batch_id, user)
    items = _items(db, batch)
    cfg = mailer.resolve(db, user.id)
    return {**_batch_out(batch), "counts": _counts(items), "items": items, "email_configured": cfg is not None}


def _recent_mail(db: Session, request_id: str) -> bool:
    return bool(db.scalar(select(func.count()).select_from(EmailOutbox).where(
        EmailOutbox.request_id == request_id, EmailOutbox.kind.in_(("invite", "reminder")),
        EmailOutbox.status.in_(("pending", "sent")), EmailOutbox.created_at > utc_now() - timedelta(hours=1))))


def _require_mail(db: Session, user: User) -> None:
    if mailer.resolve(db, user.id) is None:
        raise HTTPException(409, "Set up your email in Settings first.")


@router.post("/{batch_id}/remind")
def remind_pending(batch_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    batch = _owned(db, batch_id, user)
    _require_mail(db, user)
    queued = skipped = 0
    for item, req in db.execute(select(BulkItem, SignatureRequest).join(SignatureRequest, SignatureRequest.id == BulkItem.request_id)
                                .where(BulkItem.batch_id == batch.id)).all():
        if _status(req) != "pending":
            continue
        if _recent_mail(db, req.id):
            skipped += 1
            continue
        enqueue(db, req, "reminder", item.email, batch.id)
        add_event(db, req, "reminder_queued", f"by={user.email}")
        queued += 1
    db.commit()
    return {"queued": queued, "skipped_recently_emailed": skipped}


@router.post("/{batch_id}/retry-failed")
def retry_failed(batch_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    batch = _owned(db, batch_id, user)
    _require_mail(db, user)
    queued = 0
    for it in _items(db, batch):
        if it["email_status"] == "failed" and it["status"] == "pending":
            req = db.get(SignatureRequest, it["request_id"])
            enqueue(db, req, "invite", it["email"], batch.id)
            queued += 1
    db.commit()
    return {"queued": queued}


@router.post("/{batch_id}/items/{item_id}/resend")
def resend_item(batch_id: str, item_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    batch = _owned(db, batch_id, user)
    item = db.get(BulkItem, item_id)
    if not item or item.batch_id != batch.id:
        raise HTTPException(404, "Recipient not found")
    _require_mail(db, user)
    req = db.get(SignatureRequest, item.request_id)
    if _status(req) != "pending":
        raise HTTPException(409, f"This request is {_status(req)}.")
    if _recent_mail(db, req.id):
        raise HTTPException(429, "This recipient was emailed within the last hour.")
    enqueue(db, req, "reminder", item.email, batch.id)
    db.commit()
    return {"queued": True}


@router.post("/{batch_id}/cancel")
def cancel_pending(batch_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    batch = _owned(db, batch_id, user)
    cancelled = 0
    for item, req in db.execute(select(BulkItem, SignatureRequest).join(SignatureRequest, SignatureRequest.id == BulkItem.request_id)
                                .where(BulkItem.batch_id == batch.id)).all():
        claimed = db.execute(update(SignatureRequest).where(SignatureRequest.id == req.id, SignatureRequest.status == "pending")
                             .values(status="cancelled"))
        if claimed.rowcount == 1:
            add_event(db, req, "cancelled", f"by={user.email}; bulk={batch.id}")
            emit(db, user.id, "signature.cancelled", {"request_id": req.id, "document_id": req.document_id})
            cancelled += 1
    db.execute(update(EmailOutbox).where(EmailOutbox.batch_id == batch.id, EmailOutbox.status == "pending")
               .values(status="cancelled", last_error="Batch cancelled"))
    db.commit()
    return {"cancelled": cancelled}


# ---------------- exports ----------------

def _cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text   # spreadsheet formula injection


def _csv(header: list[str], rows: list[list], filename: str) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for r in rows:
        writer.writerow([_cell(c) for c in r])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _slug(batch: BulkBatch) -> str:
    return re.sub(r"[^\w\-]+", "-", batch.name).strip("-")[:60] or "bulk"


@router.get("/{batch_id}/export.csv")
def export_status(batch_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    batch = _owned(db, batch_id, user)
    items = _items(db, batch)
    return _csv(["row", "name", "email", "status", "email_status", "reminders", "signed_at", "signed_file_sha256"],
                [[i["row"], i["name"], i["email"], i["status"], i["email_status"], i["reminders"], i["signed_at"], i["signed_file_hash"]] for i in items],
                f"{_slug(batch)}-status.csv")


@router.get("/{batch_id}/links.csv")
def export_links(batch_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Signing links for every recipient: for sharing by WhatsApp or another channel when email isn't set up."""
    batch = _owned(db, batch_id, user)
    return _csv(["row", "name", "email", "status", "signing_link"],
                [[i["row"], i["name"], i["email"], i["status"], signing_link(i["request_id"]) if i["status"] == "pending" else ""] for i in _items(db, batch)],
                f"{_slug(batch)}-links.csv")


@router.get("/{batch_id}/signed.zip")
def download_signed(batch_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    batch = _owned(db, batch_id, user)
    signed = [i for i in _items(db, batch) if i["status"] == "signed" and i["signed_document_id"]]
    if not signed:
        raise HTTPException(409, "Nothing has been signed yet.")
    buf, used = io.BytesIO(), set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for i in signed:
            doc = db.get(Document, i["signed_document_id"])
            base = safe_stem(i["name"] or i["email"], f"row-{i['row']}")
            name, k = base, 2
            while name.lower() in used:
                name, k = f"{base}-{k}", k + 1
            used.add(name.lower())
            z.write(verified_path(doc), f"{name}.pdf")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{_slug(batch)}-signed.zip"'})
