import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pymupdf as fitz
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Document, DocumentText, Job, User
from app.security import get_current_user
from app.services.access import get_owned_document, read_upload
from app.services.scanner_service import images_to_pdf
from app.services.signing_service import apply_signature, render_page
from app.services.storage import (
    InvalidDocument,
    hash_file,
    new_path,
    register_pdf,
    resolve,
)

router = APIRouter(prefix="/documents", tags=["Documents"])


def doc_out(d: Document) -> dict:
    return {
        "id": d.id,
        "filename": d.filename,
        "pages": d.pages,
        "source": d.source,
        "parent_id": d.parent_id,
        "created_at": d.created_at.isoformat(),
    }


def verified_path(doc: Document) -> Path:
    try:
        path = resolve(doc.file_path)
    except InvalidDocument:
        raise HTTPException(404, "File unavailable")
    if not path.is_file():
        raise HTTPException(404, "File unavailable")
    if hash_file(path) != doc.file_hash:
        raise HTTPException(409, "Document integrity check failed")
    return path


@router.post("", status_code=201)
def upload(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Upload a PDF, or a single JPG/PNG image which is converted to PDF."""
    data = read_upload(file)
    path, rel = new_path("uploads")
    try:
        if data.startswith(b"%PDF"):
            path.write_bytes(data)
        elif data[:3] == b"\xff\xd8\xff" or data.startswith(b"\x89PNG"):
            with tempfile.NamedTemporaryFile(dir=settings.storage_root / "tmp") as tmp:
                tmp.write(data)
                tmp.flush()
                images_to_pdf([Path(tmp.name)], path)
        else:
            raise InvalidDocument("Only PDF, JPG and PNG files are accepted.")
        doc = register_pdf(
            db, user_id=user.id, path=path, relative=rel,
            filename=file.filename or "document.pdf", source="uploaded",
        )
    except InvalidDocument as e:
        path.unlink(missing_ok=True)
        raise HTTPException(422, str(e))
    if not doc.filename.lower().endswith(".pdf"):
        doc.filename = f"{Path(doc.filename).stem}.pdf"
    db.commit()
    return doc_out(doc)


@router.get("")
def list_documents(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    docs = db.scalars(
        select(Document).where(Document.user_id == user.id).order_by(Document.created_at.desc())
    ).all()
    return [doc_out(d) for d in docs]


@router.get("/{document_id}/download")
def download(
    document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    doc = get_owned_document(db, document_id, user.id)
    return FileResponse(verified_path(doc), media_type="application/pdf", filename=doc.filename)


@router.get("/{document_id}/pages/{page}.png")
def page_image(
    document_id: str, page: int, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    doc = get_owned_document(db, document_id, user.id)
    try:
        png = render_page(verified_path(doc), page)
    except InvalidDocument as e:
        raise HTTPException(404, str(e))
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=60"})


@router.post("/{document_id}/sign", status_code=201)
def sign_own(
    document_id: str,
    signature: UploadFile = File(...),
    page_number: int = Form(0),
    x: float = Form(...),
    y: float = Form(...),
    width: float = Form(...),
    signer_name: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Sign your own document. The original is kept; a signed copy is created."""
    doc = get_owned_document(db, document_id, user.id)
    src = verified_path(doc)
    png = read_upload(signature, limit_mb=1)
    name = (signer_name.strip() or user.name or user.email)[:120]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out, rel = new_path("results")
    try:
        apply_signature(
            src, out, signature_png=png, page_number=page_number, x=x, y=y,
            width=width, label=f"Signed by {name} · {stamp}",
        )
        signed = register_pdf(
            db, user_id=user.id, path=out, relative=rel,
            filename=f"{Path(doc.filename).stem}-signed.pdf", source="signed",
            parent_id=doc.id,
        )
    except InvalidDocument as e:
        out.unlink(missing_ok=True)
        raise HTTPException(422, str(e))
    db.commit()
    return doc_out(signed)


def _enqueue(db: Session, doc: Document, user: User, service: str) -> dict:
    busy = db.scalar(
        select(Job.id).where(
            Job.document_id == doc.id, Job.service == service,
            Job.status.in_(("queued", "processing")),
        )
    )
    if busy:
        raise HTTPException(409, "This document already has a job in progress.")
    job = Job(user_id=user.id, document_id=doc.id, service=service)
    db.add(job)
    db.commit()
    return {"job_id": job.id, "status": job.status}


@router.post("/{document_id}/tag", status_code=202)
def request_tagging(
    document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Queue accessibility tagging; the worker produces a new tagged document."""
    return _enqueue(db, get_owned_document(db, document_id, user.id), user, "pdf_tagging")


@router.post("/{document_id}/ocr", status_code=202)
def request_ocr(
    document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Queue OCR; the worker produces a searchable PDF and extractable text."""
    doc = get_owned_document(db, document_id, user.id)
    if doc.pages > settings.ocr_max_pages:
        raise HTTPException(422, f"OCR supports up to {settings.ocr_max_pages} pages per document.")
    return _enqueue(db, doc, user, "ocr")


@router.get("/{document_id}/text")
def document_text(
    document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Text per page: stored OCR output if this is an OCR result, else the PDF's own text layer."""
    doc = get_owned_document(db, document_id, user.id)
    rows = db.scalars(
        select(DocumentText).where(DocumentText.document_id == doc.id).order_by(DocumentText.page)
    ).all()
    if rows:
        return {
            "document_id": doc.id, "source": "ocr",
            "pages": [{"page": r.page, "text": r.text, "confidence": r.confidence} for r in rows],
        }
    with fitz.open(verified_path(doc)) as pdf:
        pages = [{"page": i, "text": p.get_text().strip()} for i, p in enumerate(pdf)]
    return {
        "document_id": doc.id, "source": "pdf", "pages": pages,
        "has_text": any(p["text"] for p in pages),
    }
