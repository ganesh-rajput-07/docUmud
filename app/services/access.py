"""Shared helpers for routers: ownership checks and upload reading."""
from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Document


def get_owned_document(db: Session, document_id: str, user_id: str) -> Document:
    doc = db.get(Document, document_id)
    if not doc or doc.user_id != user_id:
        raise HTTPException(404, "Document not found")
    return doc


def read_upload(file: UploadFile, limit_mb: int | None = None) -> bytes:
    limit = (limit_mb or settings.max_upload_mb) * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File exceeds the {limit_mb or settings.max_upload_mb} MB limit.")
    if not data:
        raise HTTPException(400, "The file is empty.")
    return data
