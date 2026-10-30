import json
import re
from typing import Literal

import pymupdf
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Dataset, Document, DocumentTag, FillTemplate, User, utc_now
from app.routers.datasets import dataset_out, get_owned_dataset, load_rows
from app.routers.documents import doc_out, verified_path
from app.security import get_current_user
from app.services.access import get_owned_document
from app.services.dataset_service import auto_match
from app.services.fill_service import (
    FillReport, Style, build_zip, fill_document, finish, safe_stem,
)
from app.services.fonts import DEFAULT_FONT, plain_space_font, resolve_font
from app.services.storage import InvalidDocument, new_path, register_pdf

router = APIRouter(prefix="/documents/{document_id}", tags=["Fill from data"])

HEX = r"^#[0-9a-fA-F]{6}$"


class TemplateIn(BaseModel):
    font: str | None = Field(default=None, max_length=80, description="bundled:<name> or user:<id>")
    color: str | None = Field(default=None, pattern=HEX)
    align: Literal["left", "center", "right"] | None = None
    max_font_size: int | None = Field(default=None, ge=0, le=200)
    mapping: dict[str, str | None] | None = Field(default=None, description="tag label -> column (null = use the tag's fixed value)")
    dataset_id: str | None = None
    filename_column: str | None = Field(default=None, max_length=200)


class FillIn(BaseModel):
    start: int = Field(default=1, ge=1, description="First data row (1-based)")
    end: int | None = Field(default=None, ge=1, description="Last data row, inclusive (default: all)")
    mode: Literal["zip", "merged"] = "zip"


def _tags(db: Session, doc: Document) -> list[DocumentTag]:
    return list(db.scalars(
        select(DocumentTag).where(DocumentTag.document_id == doc.id)
        .order_by(DocumentTag.page, DocumentTag.y, DocumentTag.x)
    ).all())


def _template(db: Session, doc: Document, user: User, create: bool = False) -> FillTemplate:
    t = db.scalar(select(FillTemplate).where(FillTemplate.document_id == doc.id))
    if t:
        return t
    t = FillTemplate(document_id=doc.id, user_id=user.id, font=DEFAULT_FONT, color="#111111",
                     align="left", max_font_size=0, mapping="{}")
    if create:
        db.add(t)
        db.flush()
    return t


def _dataset_of(db: Session, tmpl: FillTemplate, user: User) -> Dataset | None:
    ds = db.get(Dataset, tmpl.dataset_id) if tmpl.dataset_id else None
    return ds if ds and ds.user_id == user.id else None


def _effective_mapping(tmpl: FillTemplate, labels: list[str], columns: list[str]) -> dict[str, str | None]:
    saved = json.loads(tmpl.mapping or "{}")
    auto = auto_match(labels, columns)
    out = {}
    for label in labels:
        col = saved[label] if label in saved else auto.get(label)
        out[label] = col if col in columns else None
    return out


def _style(db: Session, tmpl: FillTemplate, user: User, report: FillReport) -> Style:
    try:
        path = resolve_font(db, user.id, tmpl.font)
    except InvalidDocument:
        path = resolve_font(db, user.id, DEFAULT_FONT)
        report.warnings.append("Your selected font was removed; using Lato instead.")
    return Style(font_path=plain_space_font(path), color=tmpl.color, align=tmpl.align, max_font_size=tmpl.max_font_size)


def _row_values(tags: list[DocumentTag], mapping: dict[str, str | None], columns: list[str], row: list[str]) -> list[str]:
    index = {c: i for i, c in enumerate(columns)}
    return [
        (row[index[mapping[t.label]]] if mapping.get(t.label) else t.value) for t in tags
    ]


@router.get("/template")
def get_template(document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(db, document_id, user.id)
    tags = _tags(db, doc)
    tmpl = _template(db, doc, user)
    ds = _dataset_of(db, tmpl, user)
    columns = json.loads(ds.columns) if ds else []
    labels = list(dict.fromkeys(t.label for t in tags))
    return {
        "document_id": doc.id, "filename": doc.filename, "pages": doc.pages,
        "style": {"font": tmpl.font, "color": tmpl.color, "align": tmpl.align, "max_font_size": tmpl.max_font_size},
        "dataset": dataset_out(ds) if ds else None,
        "fields": [{"label": l, "count": sum(1 for t in tags if t.label == l)} for l in labels],
        "mapping": _effective_mapping(tmpl, labels, columns),
        "filename_column": tmpl.filename_column if tmpl.filename_column in columns else None,
    }


@router.put("/template")
def save_template(
    document_id: str, body: TemplateIn,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Save style, data sheet and field mapping once; reuse for every run."""
    doc = get_owned_document(db, document_id, user.id)
    tmpl = _template(db, doc, user, create=True)
    sent = body.model_fields_set
    if body.font is not None:
        try:
            resolve_font(db, user.id, body.font)
        except InvalidDocument as e:
            raise HTTPException(422, str(e))
        tmpl.font = body.font
    if body.color is not None:
        tmpl.color = body.color.lower()
    if body.align is not None:
        tmpl.align = body.align
    if body.max_font_size is not None:
        tmpl.max_font_size = body.max_font_size
    if "dataset_id" in sent:
        if body.dataset_id is None:
            tmpl.dataset_id = None
        else:
            tmpl.dataset_id = get_owned_dataset(db, body.dataset_id, user.id).id
            tmpl.mapping = "{}"            # new sheet: re-match columns automatically
            tmpl.filename_column = None
    ds = _dataset_of(db, tmpl, user)
    columns = json.loads(ds.columns) if ds else []
    if body.mapping is not None:
        bad = [c for c in body.mapping.values() if c is not None and c not in columns]
        if bad:
            raise HTTPException(422, f"Unknown column(s): {', '.join(bad)}")
        tmpl.mapping = json.dumps(body.mapping)
    if "filename_column" in sent:
        if body.filename_column is not None and body.filename_column not in columns:
            raise HTTPException(422, "Unknown filename column.")
        tmpl.filename_column = body.filename_column
    tmpl.updated_at = utc_now()
    db.commit()
    return get_template(document_id, db, user)


def _prepare(db: Session, doc: Document, user: User):
    tags = _tags(db, doc)
    if not tags:
        raise HTTPException(409, "Tag at least one field on this document first.")
    tmpl = _template(db, doc, user)
    ds = _dataset_of(db, tmpl, user)
    columns = json.loads(ds.columns) if ds else []
    labels = list(dict.fromkeys(t.label for t in tags))
    return tags, tmpl, ds, columns, _effective_mapping(tmpl, labels, columns)


@router.get("/fill/preview.png")
def preview(
    document_id: str,
    row: int = Query(1, ge=1),
    page: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Render one page filled with one data row (or with the field names if no sheet is chosen)."""
    doc = get_owned_document(db, document_id, user.id)
    src = verified_path(doc)
    tags, tmpl, ds, columns, mapping = _prepare(db, doc, user)
    if page >= doc.pages:
        raise HTTPException(404, "Page out of range")
    if ds:
        rows = load_rows(ds)
        if row > len(rows):
            raise HTTPException(404, "Row out of range")
        values = _row_values(tags, mapping, columns, rows[row - 1])
    else:
        values = [t.value or t.label for t in tags]
    report = FillReport()
    pdf = fill_document(src, tags, values, _style(db, tmpl, user, report), report)
    try:
        png = pdf[page].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).tobytes("png")
    finally:
        pdf.close()
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/fill")
def fill(
    document_id: str, body: FillIn,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Generate one filled PDF per data row: a ZIP download, or one merged PDF saved to your documents."""
    doc = get_owned_document(db, document_id, user.id)
    src = verified_path(doc)
    tags, tmpl, ds, columns, mapping = _prepare(db, doc, user)
    if not ds:
        raise HTTPException(409, "Choose a data sheet first.")
    rows = load_rows(ds)
    end = min(body.end or len(rows), len(rows))
    if body.start > end:
        raise HTTPException(422, f"Row range is empty (the sheet has {len(rows)} rows).")
    chosen = rows[body.start - 1:end]
    if len(chosen) > settings.fill_max_rows:
        raise HTTPException(422, f"At most {settings.fill_max_rows} PDFs per run. Choose a smaller row range.")
    if body.mode == "merged" and len(chosen) * doc.pages > settings.max_pdf_pages:
        raise HTTPException(422, f"A merged PDF would have {len(chosen) * doc.pages} pages (limit {settings.max_pdf_pages}). Use ZIP or fewer rows.")

    report = FillReport()
    style = _style(db, tmpl, user, report)
    name_idx = columns.index(tmpl.filename_column) if tmpl.filename_column in columns else None
    stem = doc.filename.removesuffix(".pdf")

    if body.mode == "merged":
        merged = pymupdf.open()
        for row in chosen:
            part = fill_document(src, tags, _row_values(tags, mapping, columns, row), style, report)
            merged.insert_pdf(part)
            part.close()
        out, rel = new_path("results")
        out.write_bytes(finish(merged))
        merged.close()
        try:
            result = register_pdf(db, user_id=user.id, path=out, relative=rel,
                                  filename=f"{stem}-filled.pdf", source="filled", parent_id=doc.id)
        except InvalidDocument as e:
            raise HTTPException(422, str(e))
        db.commit()
        return {"mode": "merged", "rows": len(chosen), "overflow": report.overflow,
                "warnings": report.warnings, "document": doc_out(result)}

    files, used = [], set()
    for n, row in enumerate(chosen, body.start):
        pdf = fill_document(src, tags, _row_values(tags, mapping, columns, row), style, report)
        data = finish(pdf)
        pdf.close()
        base = safe_stem(row[name_idx], f"{stem}-{n:03d}") if name_idx is not None else f"{stem}-{n:03d}"
        name, k = base, 2
        while name.lower() in used:
            name, k = f"{base}-{k}", k + 1
        used.add(name.lower())
        files.append((f"{name}.pdf", data))
    zip_name = re.sub(r'[^\w.\- ]', "_", f"{stem}-filled.zip")
    return Response(
        build_zip(files), media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
            "X-DocUMud-Rows": str(len(files)),
            "X-DocUMud-Overflow": str(report.overflow),
            "X-DocUMud-Warnings": "; ".join(report.warnings),
            "Access-Control-Expose-Headers": "X-DocUMud-Rows, X-DocUMud-Overflow, X-DocUMud-Warnings, Content-Disposition",
        },
    )
