"""Queued signature emails: enqueue from request handlers, deliver from the worker."""
import logging
import time
from datetime import timedelta
from math import ceil

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import (
    BulkBatch, BulkItem, Document, EmailOutbox, SignatureRequest, SmtpConfig, User, aware, utc_now,
)
from app.services import mailer
from app.services.signing_service import add_event, hash_token, token_for
from app.services.storage import resolve

logger = logging.getLogger("documud.outbox")

BACKOFF_SECONDS = (60, 300, 1800, 7200, 21600)


def signing_link(request_id: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/sign/{token_for(request_id)}"


def enqueue(db: Session, req: SignatureRequest, kind: str, to: str, batch_id: str | None = None) -> EmailOutbox:
    """Add an email to the outbox (caller commits). Marked not_configured if the sender can't send mail."""
    configured = mailer.resolve(db, req.sender_id) is not None
    row = EmailOutbox(
        user_id=req.sender_id, request_id=req.id, batch_id=batch_id, kind=kind, to_email=to,
        status="pending" if configured else "not_configured",
    )
    db.add(row)
    return row


def sent_last_24h(db: Session, user_id: str) -> int:
    return db.scalar(
        select(func.count()).select_from(EmailOutbox).where(
            EmailOutbox.user_id == user_id, EmailOutbox.status == "sent",
            EmailOutbox.sent_at > utc_now() - timedelta(hours=24),
        )
    ) or 0


def _note_user_cfg(db: Session, cfg: mailer.SmtpSettings, user_id: str, error: str | None) -> None:
    if cfg.source != "user":
        return
    rec = db.scalar(select(SmtpConfig).where(SmtpConfig.user_id == user_id))
    if rec:
        rec.last_error = error
        if error is None and rec.verified_at is None:
            rec.verified_at = utc_now()


def _fail(db: Session, row: EmailOutbox, req: SignatureRequest | None, err: mailer.MailError) -> None:
    row.attempts += 1
    row.last_error = str(err)[:500]
    if err.permanent or row.attempts >= settings.email_max_attempts:
        row.status = "failed"
    else:
        row.next_attempt_at = utc_now() + timedelta(seconds=BACKOFF_SECONDS[min(row.attempts - 1, len(BACKOFF_SECONDS) - 1)])
    if req is not None:
        add_event(db, req, "email_failed", f"kind={row.kind}; to={row.to_email}; {row.last_error}")
    db.commit()


def _compose(db: Session, cfg: mailer.SmtpSettings, row: EmailOutbox, req: SignatureRequest):
    doc = db.get(Document, req.document_id)
    sender = db.get(User, req.sender_id)
    sender_name = sender.name or sender.email
    attachments = []
    if row.kind in ("invite", "reminder"):
        token_hash_ok = hash_token(token_for(req.id)) == req.token_hash
        if not token_hash_ok:
            raise mailer.MailError("This request was created before resend support; its link can't be rebuilt.", True)
        batch = db.get(BulkBatch, row.batch_id) if row.batch_id else None
        days = max(1, ceil((aware(req.expires_at) - utc_now()).total_seconds() / 86400))
        subject, text, html = mailer.invite_email(
            sender=sender_name, filename=doc.filename, link=signing_link(req.id), days=days,
            recipient=req.recipient_name, custom=batch.message if batch else "",
            subject=batch.subject if batch else "", reminder=row.kind == "reminder",
        )
    else:
        signed = db.get(Document, req.signed_document_id) if req.signed_document_id else None
        if not signed:
            raise mailer.MailError("The signed copy is not available.", True)
        attachments = [(signed.filename, resolve(signed.file_path).read_bytes(), "application/pdf")]
        subject, text, html = mailer.signed_email(
            sender=sender_name, signer=req.recipient_name or req.recipient_email,
            filename=doc.filename, to_signer=row.kind == "signed_signer",
        )
    return mailer.build_message(cfg, row.to_email, subject, text, html, reply_to=sender.email, attachments=attachments)


def _send_row(db: Session, smtp, cfg: mailer.SmtpSettings, row: EmailOutbox) -> None:
    req = db.get(SignatureRequest, row.request_id)
    if req is None or (row.kind in ("invite", "reminder") and req.status != "pending"):
        row.status, row.last_error = "cancelled", f"Request is {req.status if req else 'gone'}"
        db.commit()
        return
    try:
        msg = _compose(db, cfg, row, req)
        mailer.send(smtp, cfg, msg)
    except mailer.MailError as err:
        _fail(db, row, req, err)
        return
    except OSError as err:  # e.g. the signed file went missing
        _fail(db, row, req, mailer.MailError(f"File error: {err}", True))
        return
    row.attempts += 1
    row.status, row.sent_at, row.last_error = "sent", utc_now(), None
    add_event(db, req, "email_sent", f"kind={row.kind}; to={row.to_email}")
    db.commit()


def _deliver_for_user(db: Session, user_id: str, rows: list[EmailOutbox]) -> None:
    cfg = mailer.resolve(db, user_id)
    if cfg is None:
        for r in rows:
            r.status = "not_configured"
        db.commit()
        return
    if cfg.daily_limit:
        room = max(cfg.daily_limit - sent_last_24h(db, user_id), 0)
        for r in rows[room:]:      # over the daily cap: try again later, without using up an attempt
            r.next_attempt_at = utc_now() + timedelta(hours=1)
            r.last_error = f"Daily limit of {cfg.daily_limit} reached; will retry automatically."
        rows = rows[:room]
        db.commit()
    if not rows:
        return
    try:
        with mailer.session(cfg) as smtp:
            for r in rows:
                _send_row(db, smtp, cfg, r)
                time.sleep(settings.email_delay_seconds)
        _note_user_cfg(db, cfg, user_id, None)
    except mailer.MailError as err:
        for r in rows:
            if r.status == "pending":
                _fail(db, r, db.get(SignatureRequest, r.request_id), err)
        _note_user_cfg(db, cfg, user_id, str(err))
    db.commit()


def deliver_emails(limit: int = 20) -> int:
    """Send due emails, one connection per sender. Returns how many were attempted."""
    with SessionLocal() as db:
        due = db.scalars(
            select(EmailOutbox)
            .where(EmailOutbox.status == "pending", EmailOutbox.next_attempt_at <= utc_now())
            .order_by(EmailOutbox.next_attempt_at).limit(limit)
        ).all()
        by_user: dict[str, list[EmailOutbox]] = {}
        for row in due:
            by_user.setdefault(row.user_id, []).append(row)
        for user_id, rows in by_user.items():
            _deliver_for_user(db, user_id, rows)
        return len(due)


def schedule_reminders() -> int:
    """Queue a reminder for signers who haven't signed after `reminder_after_days` (up to `max_reminders`)."""
    if not settings.auto_reminders:
        return 0
    now = utc_now()
    gap = timedelta(days=settings.reminder_after_days)
    with SessionLocal() as db:
        reqs = db.scalars(
            select(SignatureRequest).where(
                SignatureRequest.status == "pending", SignatureRequest.mode == "email",
                SignatureRequest.recipient_email != "", SignatureRequest.expires_at > now,
                SignatureRequest.created_at <= now - gap,
            )
        ).all()
        if not reqs:
            return 0
        ids = [r.id for r in reqs]
        mails = db.scalars(
            select(EmailOutbox).where(
                EmailOutbox.request_id.in_(ids), EmailOutbox.kind.in_(("invite", "reminder")),
                EmailOutbox.status.in_(("sent", "pending")),
            )
        ).all()
        last: dict[str, object] = {}
        count: dict[str, int] = {}
        for m in mails:
            last[m.request_id] = max(last.get(m.request_id, aware(m.created_at)), aware(m.created_at))
            count[m.request_id] = count.get(m.request_id, 0) + (m.kind == "reminder")
        batches = dict(db.execute(select(BulkItem.request_id, BulkItem.batch_id).where(BulkItem.request_id.in_(ids))).all())
        queued = 0
        for r in reqs:
            if r.id not in last or count.get(r.id, 0) >= settings.max_reminders:
                continue   # never invited by email, or enough reminders already
            if now - last[r.id] >= gap:
                enqueue(db, r, "reminder", r.recipient_email, batches.get(r.id))
                queued += 1
        db.commit()
        return queued
