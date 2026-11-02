"""Paginated document library for large accounts."""
import base64
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Document, User, aware
from app.routers.documents import doc_out
from app.security import get_current_user

router = APIRouter(prefix="/v1", tags=["Documents"])


@router.get("/documents", summary="List documents (cursor-paginated, searchable)")
def list_documents(
    q: str | None = Query(None, description="Matches the file name"),
    source: str | None = Query(None, description="uploaded | scanned | signed | tagged | ocr | filled | envelope (comma-separate several)"),
    include_batch: bool = False, include_total: bool = False,
    limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = select(Document).where(Document.user_id == user.id)
    if not include_batch:
        query = query.where(Document.source.not_in(("batch", "batch-signed")))
    if source:
        query = query.where(Document.source.in_([s.strip() for s in source.split(",") if s.strip()]))
    if q and q.strip():
        query = query.where(Document.filename.ilike(f"%{q.strip()}%"))
    total = db.scalar(select(func.count()).select_from(query.subquery())) if include_total else None
    if cursor:
        try:
            ts, id_ = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
            ts = datetime.fromisoformat(ts)
        except Exception:
            raise HTTPException(422, "Invalid cursor.")
        query = query.where(or_(Document.created_at < ts, (Document.created_at == ts) & (Document.id < id_)))
    rows = db.scalars(query.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit + 1)).all()
    page = rows[:limit]
    nxt = base64.urlsafe_b64encode(f"{aware(page[-1].created_at).isoformat()}|{page[-1].id}".encode()).decode() if len(rows) > limit else None
    return {"data": [doc_out(d) for d in page], "has_more": len(rows) > limit, "next_cursor": nxt, **({"total": total} if total is not None else {})}
