"""Private file storage + Document record creation."""
import hashlib
import re
import uuid
from pathlib import Path

import pymupdf as fitz
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Document


class InvalidDocument(ValueError):
    """Raised for user-correctable problems with an uploaded/generated file."""


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve(relative: str) -> Path:
    """Resolve a stored path, refusing anything outside the storage root."""
    root = settings.storage_root.resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise InvalidDocument("Invalid storage path")
    return path


def safe_filename(name: str) -> str:
    name = Path(name or "document.pdf").name
    name = re.sub(r"[^\w.\- ]", "_", name).strip() or "document.pdf"
    return name[:200]


def validate_pdf(path: Path) -> int:
    """Return the page count, or raise InvalidDocument."""
    try:
        with fitz.open(path) as pdf:
            if pdf.needs_pass:
                raise InvalidDocument("Password-protected PDFs are not supported.")
            pages = pdf.page_count
    except InvalidDocument:
        raise
    except Exception:
        raise InvalidDocument("The file is not a valid PDF.")
    if pages < 1:
        raise InvalidDocument("The PDF has no pages.")
    if pages > settings.max_pdf_pages:
        raise InvalidDocument(f"PDF exceeds the {settings.max_pdf_pages}-page limit.")
    return pages


def new_path(folder: str) -> tuple[Path, str]:
    """Return (absolute path, relative path) for a fresh PDF in `folder`."""
    rel = f"{folder}/{uuid.uuid4()}.pdf"
    return settings.storage_root / rel, rel


def register_pdf(
    db: Session,
    *,
    user_id: str,
    path: Path,
    relative: str,
    filename: str,
    source: str,
    parent_id: str | None = None,
) -> Document:
    """Validate an already-written PDF and create its Document row."""
    try:
        pages = validate_pdf(path)
    except InvalidDocument:
        path.unlink(missing_ok=True)
        raise
    doc = Document(
        user_id=user_id,
        filename=safe_filename(filename),
        file_path=relative,
        file_hash=hash_file(path),
        pages=pages,
        source=source,
        parent_id=parent_id,
    )
    db.add(doc)
    db.flush()
    return doc
