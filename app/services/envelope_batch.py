"""Background creation of many envelopes from one API request."""
import json
import logging

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import EnvelopeBatch, Template, User, utc_now
from app.services import envelope_service as svc
from app.services.storage import resolve
from app.services.webhook_service import emit

logger = logging.getLogger("documud.batch")

OPTION_KEYS = ("message", "signing_order", "expires_in_days", "send", "office_auto_sign", "strict", "return_url")


def results_path(batch: EnvelopeBatch):
    return resolve(batch.payload_path).with_suffix(".results.jsonl")


def process_next_chunk() -> int:
    """Create up to `batch_chunk` envelopes for the oldest unfinished batch. Returns how many were attempted."""
    with SessionLocal() as db:
        batch = db.scalar(select(EnvelopeBatch).where(EnvelopeBatch.status.in_(("queued", "processing")))
                          .order_by(EnvelopeBatch.created_at).limit(1))
        if batch is None:
            return 0
        items = json.loads(resolve(batch.payload_path).read_text(encoding="utf-8"))
        start = batch.created_count + batch.failed_count
        chunk = items[start:start + settings.batch_chunk]
        batch.status = "processing"
        db.commit()
        user, template = db.get(User, batch.user_id), db.get(Template, batch.template_id)
        shared = json.loads(batch.options)
        lines = []
        for offset, item in enumerate(chunk):
            index = start + offset
            try:
                opts = {k: item.get(k, shared.get(k)) for k in OPTION_KEYS}
                env, warnings = svc.create_envelope(
                    db, user, template, recipients=item.get("recipients", []), data=item.get("data"), field_map=item.get("field_map"),
                    name=item.get("name"), message=opts["message"] or "", signing_order=opts["signing_order"] or "sequential",
                    expires_in_days=int(opts["expires_in_days"] or 14), external_id=item.get("external_id"),
                    metadata=item.get("metadata") or {}, return_url=opts["return_url"], send=opts["send"] is not False,
                    office_auto_sign=bool(opts["office_auto_sign"]), strict=bool(opts["strict"]))
                db.commit()
                batch.created_count += 1
                lines.append({"index": index, "envelope_id": env.id, "external_id": env.external_id, "warnings": warnings})
            except svc.EnvelopeError as e:
                db.rollback()
                batch.failed_count += 1
                lines.append({"index": index, "error": e.message, "errors": e.errors, "external_id": item.get("external_id")})
            except Exception as e:                               # one bad item must not stop the batch
                db.rollback()
                logger.exception("Batch %s item %s failed", batch.id, index)
                batch.failed_count += 1
                lines.append({"index": index, "error": f"Unexpected error: {e.__class__.__name__}", "external_id": item.get("external_id")})
            db.add(batch)
            db.commit()
        with results_path(batch).open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        if batch.created_count + batch.failed_count >= batch.total:
            batch.status, batch.finished_at = "completed", utc_now()
            emit(db, batch.user_id, "batch.completed", {"batch_id": batch.id, "total": batch.total,
                                                         "created": batch.created_count, "failed": batch.failed_count})
        db.commit()
        return len(chunk)
