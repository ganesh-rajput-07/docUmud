import base64
import hashlib
import hmac
import io
import secrets
from datetime import datetime, timezone
from pathlib import Path

import pymupdf as fitz
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SignatureEvent, SignatureRequest
from app.services.storage import InvalidDocument

MAX_SIGNATURE_BYTES = 1_000_000


def create_signing_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def token_for(request_id: str) -> str:
    """The signing token for a request, derived from the server secret so reminders and resends can
    rebuild the same link. Only its hash is stored."""
    mac = hmac.new(settings.secret_key.encode(), b"sign-token:" + request_id.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def render_page(path: Path, page_number: int, zoom: float = 1.5) -> bytes:
    with fitz.open(path) as pdf:
        if not 0 <= page_number < pdf.page_count:
            raise InvalidDocument("Page out of range.")
        return pdf[page_number].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")


def apply_signature(
    src: Path,
    dst: Path,
    *,
    signature_png: bytes,
    page_number: int,
    x: float,
    y: float,
    width: float,
    label: str,
) -> None:
    """Stamp a visible signature (x, y = top-left, all normalised 0–1) plus a caption.

    This is a visible mark, not a certificate-backed digital signature.
    """
    if len(signature_png) > MAX_SIGNATURE_BYTES:
        raise InvalidDocument("Signature image is too large.")
    try:
        with Image.open(io.BytesIO(signature_png)) as img:
            if img.format != "PNG":
                raise InvalidDocument("Signature must be a PNG image.")
            ratio = img.height / img.width
    except (UnidentifiedImageError, ZeroDivisionError):
        raise InvalidDocument("Signature image is invalid.")
    if not (0 < width <= 1 and 0 <= x <= 1 and 0 <= y <= 1):
        raise InvalidDocument("Invalid signature placement.")

    with fitz.open(src) as pdf:
        if not 0 <= page_number < pdf.page_count:
            raise InvalidDocument("Page out of range.")
        page = pdf[page_number]
        pw, ph = page.rect.width, page.rect.height
        w = width * pw
        h = w * ratio
        left = min(max(x * pw, 0), pw - w)
        top = min(max(y * ph, 0), ph - h - 12)
        page.insert_image(fitz.Rect(left, top, left + w, top + h), stream=signature_png)
        page.insert_text(
            fitz.Point(left, top + h + 9), label, fontsize=7, color=(0.3, 0.3, 0.3)
        )
        pdf.save(dst, garbage=3, deflate=True)


def add_event(
    db: Session, request: SignatureRequest, event_type: str, details: str = ""
) -> SignatureEvent:
    """Append an event whose hash covers the previous one (tamper-evident chain)."""
    prev = _last_hash(db, request.id)
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(
        f"{prev}|{request.id}|{event_type}|{details}|{now.isoformat()}".encode()
    ).hexdigest()
    event = SignatureEvent(
        request_id=request.id,
        event_type=event_type,
        details=details,
        prev_hash=prev,
        event_hash=digest,
        created_at=now,
    )
    db.add(event)
    return event


def _last_hash(db: Session, request_id: str) -> str:
    events = db.scalars(
        select(SignatureEvent)
        .where(SignatureEvent.request_id == request_id)
        .order_by(SignatureEvent.created_at)
    ).all()
    # autoflush is off, so include events added earlier in this session.
    pending = [
        o for o in db.new if isinstance(o, SignatureEvent) and o.request_id == request_id
    ]
    chain = events + pending
    return chain[-1].event_hash if chain else "0" * 64


def verify_chain(events: list[SignatureEvent], request_id: str) -> bool:
    prev = "0" * 64
    for e in events:
        expected = hashlib.sha256(
            f"{prev}|{request_id}|{e.event_type}|{e.details}|{e.created_at.replace(tzinfo=timezone.utc).isoformat()}".encode()
        ).hexdigest()
        if e.prev_hash != prev or not hmac.compare_digest(e.event_hash, expected):
            return False
        prev = e.event_hash
    return True