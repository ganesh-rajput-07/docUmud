from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Document, DocumentTag, User
from app.security import get_current_user
from app.routers.documents import doc_out, verified_path
from app.services.access import get_owned_document
from app.services.storage import InvalidDocument, new_path, register_pdf
from app.services.tag_export import write_tags

router = APIRouter(prefix="/documents/{document_id}/tags", tags=["Tags"])

MAX_TAGS = 500


class TagIn(BaseModel):
    """Region on a page; x, y, w, h are fractions (0–1) of the page, origin top-left."""

    page: int = Field(ge=0)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(default="", max_length=2000)


class TagPatch(BaseModel):
    page: int | None = Field(default=None, ge=0)
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    w: float | None = Field(default=None, gt=0, le=1)
    h: float | None = Field(default=None, gt=0, le=1)
    label: str | None = Field(default=None, min_length=1, max_length=80)
    value: str | None = Field(default=None, max_length=2000)


def _out(t: DocumentTag) -> dict:
    return {
        "id": t.id, "page": t.page, "x": t.x, "y": t.y, "w": t.w, "h": t.h,
        "label": t.label, "value": t.value, "created_at": t.created_at.isoformat(),
    }


def _check_bounds(doc: Document, page: int, x: float, y: float, w: float, h: float) -> None:
    if page >= doc.pages:
        raise HTTPException(422, f"Page out of range (document has {doc.pages} pages).")
    if x + w > 1.0001 or y + h > 1.0001:
        raise HTTPException(422, "Tag must lie within the page.")


def _get_tag(db: Session, doc: Document, tag_id: str) -> DocumentTag:
    tag = db.get(DocumentTag, tag_id)
    if not tag or tag.document_id != doc.id:
        raise HTTPException(404, "Tag not found")
    return tag


@router.get("")
def list_tags(
    document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    doc = get_owned_document(db, document_id, user.id)
    tags = db.scalars(
        select(DocumentTag)
        .where(DocumentTag.document_id == doc.id)
        .order_by(DocumentTag.page, DocumentTag.y, DocumentTag.x)
    ).all()
    return {"document_id": doc.id, "filename": doc.filename, "pages": doc.pages, "tags": [_out(t) for t in tags]}


@router.post("", status_code=201)
def create_tag(
    document_id: str, body: TagIn,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    doc = get_owned_document(db, document_id, user.id)
    _check_bounds(doc, body.page, body.x, body.y, body.w, body.h)
    count = db.scalar(select(func.count()).select_from(DocumentTag).where(DocumentTag.document_id == doc.id))
    if count >= MAX_TAGS:
        raise HTTPException(409, f"A document can have at most {MAX_TAGS} tags.")
    tag = DocumentTag(document_id=doc.id, **body.model_dump())
    tag.label = tag.label.strip()
    db.add(tag)
    db.commit()
    return _out(tag)


@router.post("/export", status_code=201)
def export_tags_to_pdf(
    document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Create a new PDF with every tag written as an annotation (original is kept)."""
    doc = get_owned_document(db, document_id, user.id)
    tags = db.scalars(
        select(DocumentTag).where(DocumentTag.document_id == doc.id)
        .order_by(DocumentTag.page, DocumentTag.y, DocumentTag.x)
    ).all()
    if not tags:
        raise HTTPException(409, "Add at least one tag before exporting.")
    out, rel = new_path("results")
    try:
        write_tags(verified_path(doc), out, tags)
        exported = register_pdf(
            db, user_id=user.id, path=out, relative=rel,
            filename=f"{doc.filename.removesuffix('.pdf')}-tags.pdf",
            source="annotated", parent_id=doc.id,
        )
    except InvalidDocument as e:
        out.unlink(missing_ok=True)
        raise HTTPException(422, str(e))
    db.commit()
    return doc_out(exported)


@router.patch("/{tag_id}")
def update_tag(
    document_id: str, tag_id: str, body: TagPatch,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    doc = get_owned_document(db, document_id, user.id)
    tag = _get_tag(db, doc, tag_id)
    for key, val in body.model_dump(exclude_unset=True).items():
        if val is not None:
            setattr(tag, key, val.strip() if key == "label" else val)
    _check_bounds(doc, tag.page, tag.x, tag.y, tag.w, tag.h)
    db.commit()
    return _out(tag)


@router.delete("/{tag_id}", status_code=204)
def delete_tag(
    document_id: str, tag_id: str,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    doc = get_owned_document(db, document_id, user.id)
    db.delete(_get_tag(db, doc, tag_id))
    db.commit()
