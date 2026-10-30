import json
import logging
import shutil
import time

from sqlalchemy import select, update

from app.config import settings
from app.database import SessionLocal, initialize_database
from app.models import Document, DocumentText, Job
from app.services.ocr_service import ocr_pdf
from app.services.pdf_processor import ProcessingError, process_pdf
from app.services.webhook_service import deliver_due, emit
from app.services.storage import InvalidDocument, new_path, register_pdf, resolve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("documud.worker")


def recover_interrupted_jobs() -> None:
    """Single-worker setup: anything left 'processing' was interrupted by a crash."""
    with SessionLocal() as db:
        db.execute(update(Job).where(Job.status == "processing").values(status="queued"))
        db.commit()


def claim_job() -> str | None:
    with SessionLocal() as db:
        job_id = db.scalar(
            select(Job.id).where(Job.status == "queued").order_by(Job.created_at).limit(1)
        )
        if not job_id:
            return None
        claimed = db.execute(
            update(Job).where(Job.id == job_id, Job.status == "queued").values(status="processing")
        )
        db.commit()
        return job_id if claimed.rowcount == 1 else None


def _run_tagging(db, job: Job, source: Document):
    output, report = process_pdf(resolve(source.file_path), job.id)
    dest, rel = new_path("results")
    shutil.copyfile(output, dest)
    result = register_pdf(
        db, user_id=job.user_id, path=dest, relative=rel,
        filename=f"{source.filename.removesuffix('.pdf')}-tagged.pdf",
        source="tagged", parent_id=source.id,
    )
    return result, report


def _run_ocr(db, job: Job, source: Document):
    dest, rel = new_path("results")
    try:
        pages, report = ocr_pdf(resolve(source.file_path), dest)
        result = register_pdf(
            db, user_id=job.user_id, path=dest, relative=rel,
            filename=f"{source.filename.removesuffix('.pdf')}-ocr.pdf",
            source="ocr", parent_id=source.id,
        )
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    for n, page in enumerate(pages):
        db.add(DocumentText(document_id=result.id, page=n, text=page.text, confidence=page.confidence))
    return result, report


def process_job(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        event = {"job_id": job.id, "service": job.service, "document_id": job.document_id}
        try:
            source = db.get(Document, job.document_id)
            run = _run_ocr if job.service == "ocr" else _run_tagging
            result, report = run(db, job, source)
            job.result_document_id = result.id
            job.report = json.dumps(report)
            job.status = "completed"
            emit(db, job.user_id, "job.completed", {**event, "result_document_id": result.id})
        except (ProcessingError, InvalidDocument) as exc:
            db.rollback()
            logger.warning("Job %s failed: %s", job_id, exc)
            job.status, job.error = "failed", str(exc)
            emit(db, job.user_id, "job.failed", {**event, "error": job.error})
        except Exception:
            db.rollback()
            logger.exception("Job %s crashed", job_id)
            job.status, job.error = "failed", "Unexpected processing error."
            emit(db, job.user_id, "job.failed", {**event, "error": job.error})
        finally:
            shutil.rmtree(settings.storage_root / "tmp" / job_id, ignore_errors=True)
        db.commit()


def main() -> None:
    initialize_database()
    recover_interrupted_jobs()
    logger.info("DocUMud worker started")
    while True:
        job_id = claim_job()
        if job_id:
            process_job(job_id)
        elif not deliver_due():
            time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    main()
