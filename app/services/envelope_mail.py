"""Envelope email outbox: invitations, reminders and completed copies sent from the owner's mailbox."""
import logging
import time
from datetime import timedelta
from math import ceil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Document, Envelope, EnvelopeMail, EnvelopeRecipient, User, aware, utc_now
from app.services import mailer
from app.services.envelope_service import signing_url
from app.services.storage import resolve

logger = logging.getLogger("documud.envelope_mail")
BACKOFF = (60, 300, 1800, 7200, 21600)


def enqueue_mail(db: Session, env: Envelope, rec: EnvelopeRecipient | None, kind: str, to: str | None = None) -> EnvelopeMail:
    address = to or (rec.email if rec else "")
    configured = mailer.resolve(db, env.user_id) is not None
    row = EnvelopeMail(user_id=env.user_id, envelope_id=env.id, recipient_id=rec.id if rec else None, kind=kind,
                       to_email=address, status="pending" if configured else "not_configured")
    db.add(row)
    return row


def _compose(db: Session, cfg: mailer.SmtpSettings, row: EnvelopeMail, env: Envelope):
    owner = db.get(User, env.user_id)
    sender = owner.name or owner.email
    if row.kind in ("invite", "reminder"):
        rec = db.get(EnvelopeRecipient, row.recipient_id)
        days = max(1, ceil((aware(env.expires_at) - utc_now()).total_seconds() / 86400))
        subject, text, html = mailer.invite_email(sender=sender, filename=env.name, link=signing_url(rec.id), days=days,
                                                  recipient=rec.name, custom=env.message, reminder=row.kind == "reminder")
        attachments = []
    else:
        final = db.get(Document, env.final_document_id)
        subject, text, html = mailer.completed_email(sender=sender, name=env.name)
        attachments = [(final.filename, resolve(final.file_path).read_bytes(), "application/pdf")]
    return mailer.build_message(cfg, row.to_email, subject, text, html, reply_to=owner.email, attachments=attachments)


def _fail(db: Session, row: EnvelopeMail, err: mailer.MailError) -> None:
    row.attempts += 1
    row.last_error = str(err)[:500]
    if err.permanent or row.attempts >= settings.email_max_attempts:
        row.status = "failed"
    else:
        row.next_attempt_at = utc_now() + timedelta(seconds=BACKOFF[min(row.attempts - 1, len(BACKOFF) - 1)])
    db.commit()


def _deliver_user(db: Session, user_id: str, rows: list[EnvelopeMail]) -> None:
    cfg = mailer.resolve(db, user_id)
    if cfg is None:
        for r in rows:
            r.status = "not_configured"
        db.commit()
        return
    try:
        with mailer.session(cfg) as smtp:
            for row in rows:
                env = db.get(Envelope, row.envelope_id)
                if row.kind in ("invite", "reminder") and (env.status != "in_progress"):
                    row.status, row.last_error = "cancelled", f"Envelope is {env.status}"
                    db.commit()
                    continue
                try:
                    mailer.send(smtp, cfg, _compose(db, cfg, row, env))
                except mailer.MailError as err:
                    _fail(db, row, err)
                    continue
                except OSError as err:
                    _fail(db, row, mailer.MailError(f"File error: {err}", True))
                    continue
                row.attempts += 1
                row.status, row.sent_at, row.last_error = "sent", utc_now(), None
                db.commit()
                time.sleep(settings.email_delay_seconds)
    except mailer.MailError as err:
        for r in rows:
            if r.status == "pending":
                _fail(db, r, err)


def deliver_envelope_mail(limit: int = 20) -> int:
    with SessionLocal() as db:
        due = db.scalars(select(EnvelopeMail).where(EnvelopeMail.status == "pending", EnvelopeMail.next_attempt_at <= utc_now())
                         .order_by(EnvelopeMail.next_attempt_at).limit(limit)).all()
        by_user: dict[str, list[EnvelopeMail]] = {}
        for r in due:
            by_user.setdefault(r.user_id, []).append(r)
        for uid, rows in by_user.items():
            _deliver_user(db, uid, rows)
        return len(due)
