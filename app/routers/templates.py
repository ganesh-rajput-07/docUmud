import json
from typing import Any

import pymupdf
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Document, Envelope, Template, TemplateField, User, aware, utc_now
from app.routers.documents import upload as upload_document, verified_path
from app.security import get_current_user
from app.services import data_normalizer as dn
from app.services.access import get_owned_document
from app.services.field_types import FieldError, catalog
from app.services.fill_service import FillReport
from app.services.render import draw_fields, finish
from app.services.signature_images import typed_png
from app.services.template_service import (
    DefinitionError, build_fields, clean_roles, clean_style, data_schema, font_resolver, jl, sample_fields,
)

router = APIRouter(prefix="/v1", tags=["Templates"])


class AnchorIn(BaseModel):
    text: str = Field(description="Text printed on the document to position the field against")
    page: int | None = Field(default=None, description="Search only this page (1-based); default: whole document")
    occurrence: int = Field(default=1, ge=1, description="Use the Nth match")
    position: str = Field(default="right", description="right | left | below | over")
    offset_x: float = 0
    offset_y: float = 0


class FieldIn(BaseModel):
    name: str | None = Field(default=None, description="Key used in API data. Generated from the label if omitted")
    label: str = ""
    type: str = Field(description="See GET /v1/field-types")
    role: str | None = Field(default=None, description="Which role fills/signs it (default: the first role)")
    page: int = Field(default=1, ge=1)
    x: float | None = Field(default=None, description="Left edge, fraction of page width")
    y: float | None = Field(default=None, description="Top edge, fraction of page height")
    w: float | None = Field(default=None, description="Width, fraction of page width (default: the type's size)")
    h: float | None = None
    anchor: AnchorIn | None = Field(default=None, description="Place relative to printed text instead of x/y")
    required: bool = True
    default_value: str = ""
    options: list[str] = []
    option: str = Field(default="", description="radio only: the choice this box represents")
    aliases: list[str] = Field(default=[], description="Other data keys that should fill this field")
    style: dict[str, Any] = Field(default={}, description="font, font_size, color, align, uppercase, date_format, currency, decimals, grouping, mark")
    validation: dict[str, Any] = Field(default={}, description="pattern, pattern_message, min_length, max_length, min, max")


class RoleIn(BaseModel):
    id: str
    label: str = ""
    kind: str = Field(default="signer", description="office (the account owner) | signer")
    order: int | None = None


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    document_id: str | None = Field(default=None, description="An uploaded PDF (POST /documents). Required when creating")
    roles: list[RoleIn] | None = None
    defaults: dict[str, Any] = Field(default={}, description="Default font, font_size, color, align for text fields")
    fields: list[FieldIn] = []


class TemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, pattern="^(active|archived)$")
    roles: list[RoleIn] | None = None
    defaults: dict[str, Any] | None = None


# ---------------- helpers ----------------

def field_out(f: TemplateField) -> dict:
    return {"id": f.id, "name": f.name, "label": f.label, "type": f.type, "role": f.role, "page": f.page,
            "x": round(f.x, 5), "y": round(f.y, 5), "w": round(f.w, 5), "h": round(f.h, 5), "required": f.required,
            "default_value": f.default_value, "options": jl(f.options, []), "option": f.option,
            "aliases": jl(f.aliases, []), "style": jl(f.style, {}), "validation": jl(f.validation, {})}


def template_out(t: Template, fields: list[TemplateField], doc: Document | None = None) -> dict:
    return {"id": t.id, "name": t.name, "description": t.description, "status": t.status, "version": t.version,
            "document_id": t.document_id, "pages": doc.pages if doc else None, "roles": jl(t.roles, []),
            "defaults": jl(t.defaults, {}), "fields": [field_out(f) for f in fields],
            "created_at": aware(t.created_at).isoformat(), "updated_at": aware(t.updated_at).isoformat()}


def get_owned_template(db: Session, template_id: str, user: User) -> Template:
    t = db.get(Template, template_id)
    if not t or t.user_id != user.id:
        raise HTTPException(404, "Template not found")
    return t


def template_fields(db: Session, t: Template) -> list[TemplateField]:
    return list(db.scalars(select(TemplateField).where(TemplateField.template_id == t.id)
                           .order_by(TemplateField.position, TemplateField.id)).all())


def _open(doc: Document):
    return pymupdf.open(verified_path(doc))


def _full(db: Session, t: Template) -> dict:
    return template_out(t, template_fields(db, t), db.get(Document, t.document_id))


def _replace_fields(db: Session, t: Template, user: User, defs: list[dict], roles: list[dict]) -> None:
    doc = db.get(Document, t.document_id)
    with _open(doc) as pdf:
        rows = build_fields(defs, roles, pdf=pdf, pages=doc.pages, db=db, user_id=user.id)
    for old in template_fields(db, t):
        db.delete(old)
    db.flush()
    for r in rows:
        db.add(TemplateField(template_id=t.id, **r))


def _defaults(db: Session, user: User, raw: dict) -> dict:
    try:
        return clean_style(raw, db=db, user_id=user.id)
    except DefinitionError as e:
        raise HTTPException(422, f"defaults: {e}")


def _guard(fn):
    try:
        return fn()
    except DefinitionError as e:
        raise HTTPException(422, str(e))


# ---------------- reference ----------------

@router.get("/field-types", summary="List the field types you can place on a template")
def field_types(user: User = Depends(get_current_user)):
    return {"types": catalog(), "style_options": ["font", "font_size", "color", "align", "uppercase", "date_format",
            "currency", "currency_position", "decimals", "grouping", "mark"],
            "validation_options": ["pattern", "pattern_message", "min_length", "max_length", "min", "max"]}


# ---------------- templates ----------------

@router.post("/templates", status_code=201, summary="Create a template from an uploaded document and a field list")
def create_template(body: TemplateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not body.document_id:
        raise HTTPException(422, "document_id is required (upload a PDF with POST /documents, or use POST /v1/templates/upload).")
    doc = get_owned_document(db, body.document_id, user.id)
    roles = _guard(lambda: clean_roles([r.model_dump(exclude_none=True) for r in body.roles] if body.roles else None))
    t = Template(user_id=user.id, name=body.name.strip(), description=body.description, document_id=doc.id,
                 roles=json.dumps(roles), defaults=json.dumps(_defaults(db, user, body.defaults)))
    db.add(t)
    db.flush()
    _guard(lambda: _replace_fields(db, t, user, [f.model_dump() for f in body.fields], roles))
    db.commit()
    return _full(db, t)


@router.post("/templates/upload", status_code=201, summary="Upload a PDF and create its template in one call (multipart)")
def create_template_upload(
    file: UploadFile = File(...),
    definition: str = Form("{}", description="JSON: {name, description, roles, defaults, fields[]}"),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    try:
        body = TemplateIn(**{"name": (file.filename or "Template").rsplit(".", 1)[0], **json.loads(definition or "{}")})
    except (ValueError, TypeError) as e:
        raise HTTPException(422, f"definition is not valid: {e}")
    doc = upload_document(file, db, user)
    body.document_id = doc["id"]
    return create_template(body, db, user)


@router.get("/templates", summary="List templates")
def list_templates(
    status: str = Query("active", pattern="^(active|archived|all)$"), q: str = "",
    limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = select(Template).where(Template.user_id == user.id).order_by(Template.updated_at.desc()).limit(limit)
    if status != "all":
        query = query.where(Template.status == status)
    if q.strip():
        query = query.where(Template.name.ilike(f"%{q.strip()}%"))
    items = db.scalars(query).all()
    counts = dict(db.execute(select(TemplateField.template_id, func.count()).where(
        TemplateField.template_id.in_([t.id for t in items] or [""])).group_by(TemplateField.template_id)).all())
    used = dict(db.execute(select(Envelope.template_id, func.count()).where(
        Envelope.user_id == user.id, Envelope.template_id.in_([t.id for t in items] or [""])).group_by(Envelope.template_id)).all())
    return {"data": [{"id": t.id, "name": t.name, "description": t.description, "status": t.status, "version": t.version,
                      "document_id": t.document_id, "field_count": counts.get(t.id, 0), "envelope_count": used.get(t.id, 0),
                      "roles": jl(t.roles, []), "updated_at": aware(t.updated_at).isoformat()} for t in items]}


@router.get("/templates/{template_id}", summary="Get a template with its fields")
def get_template(template_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _full(db, get_owned_template(db, template_id, user))


@router.put("/templates/{template_id}", summary="Replace a template's definition (roles, defaults and the whole field list)")
def replace_template(template_id: str, body: TemplateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Existing envelopes keep the fields they were created with; only new envelopes use this version."""
    t = get_owned_template(db, template_id, user)
    roles = _guard(lambda: clean_roles([r.model_dump(exclude_none=True) for r in body.roles] if body.roles else jl(t.roles, [])))
    t.name, t.description, t.roles = body.name.strip(), body.description, json.dumps(roles)
    t.defaults = json.dumps(_defaults(db, user, body.defaults))
    _guard(lambda: _replace_fields(db, t, user, [f.model_dump() for f in body.fields], roles))
    t.version += 1
    db.commit()
    return _full(db, t)


@router.patch("/templates/{template_id}", summary="Rename, archive, or change roles/defaults")
def patch_template(template_id: str, body: TemplatePatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = get_owned_template(db, template_id, user)
    if body.name is not None:
        t.name = body.name.strip()
    if body.description is not None:
        t.description = body.description
    if body.status is not None:
        t.status = body.status
    if body.defaults is not None:
        t.defaults = json.dumps(_defaults(db, user, body.defaults))
    if body.roles is not None:
        roles = _guard(lambda: clean_roles([r.model_dump(exclude_none=True) for r in body.roles]))
        used = {f.role for f in template_fields(db, t)}
        missing = used - {r["id"] for r in roles}
        if missing:
            raise HTTPException(422, f"Fields still use role(s) {', '.join(sorted(missing))}; reassign them first.")
        t.roles = json.dumps(roles)
    t.version += 1
    db.commit()
    return _full(db, t)


@router.delete("/templates/{template_id}", status_code=204, summary="Archive a template (envelopes already created are unaffected)")
def archive_template(template_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    get_owned_template(db, template_id, user).status = "archived"
    db.commit()


@router.post("/templates/{template_id}/duplicate", status_code=201, summary="Copy a template")
def duplicate_template(template_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = get_owned_template(db, template_id, user)
    copy = Template(user_id=user.id, name=f"{t.name} (copy)"[:200], description=t.description, document_id=t.document_id,
                    roles=t.roles, defaults=t.defaults)
    db.add(copy)
    db.flush()
    for f in template_fields(db, t):
        row = {c.name: getattr(f, c.name) for c in TemplateField.__table__.columns if c.name not in ("id", "template_id")}
        db.add(TemplateField(template_id=copy.id, **row))
    db.commit()
    return _full(db, copy)


# ---------------- single-field editing (used by the designer) ----------------

@router.post("/templates/{template_id}/fields", status_code=201, summary="Add one field")
def add_field(template_id: str, body: FieldIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = get_owned_template(db, template_id, user)
    existing = template_fields(db, t)
    doc = db.get(Document, t.document_id)
    defs = [body.model_dump()]
    with _open(doc) as pdf:
        row = _guard(lambda: build_fields(defs, jl(t.roles, []), pdf=pdf, pages=doc.pages, db=db, user_id=user.id,
                                          start_position=len(existing)))[0]
    clash = next((f for f in existing if f.name == row["name"] and (row["type"] != "radio" or f.type != "radio")), None)
    if clash:
        raise HTTPException(422, f"A field named '{row['name']}' already exists.")
    if row["type"] == "radio":
        group = [f for f in existing if f.type == "radio" and f.name == row["name"]]
        if any(g.option == row["option"] for g in group):
            raise HTTPException(422, f"Radio group '{row['name']}' already has option '{row['option']}'.")
        opts = [g.option for g in group] + [row["option"]]
        row["options"] = json.dumps(opts)
        for g in group:
            g.options = row["options"]
    f = TemplateField(template_id=t.id, **row)
    db.add(f)
    t.version += 1
    db.commit()
    return field_out(f)


@router.patch("/templates/{template_id}/fields/{field_id}", summary="Change one field")
def patch_field(template_id: str, field_id: str, body: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = get_owned_template(db, template_id, user)
    f = db.get(TemplateField, field_id)
    if not f or f.template_id != t.id:
        raise HTTPException(404, "Field not found")
    current = field_out(f)
    merged = {**current, **{k: v for k, v in body.items() if k in current and k != "id"}}
    doc = db.get(Document, t.document_id)
    with _open(doc) as pdf:
        row = _guard(lambda: build_fields([merged], jl(t.roles, []), pdf=pdf, pages=doc.pages, db=db, user_id=user.id,
                                          start_position=f.position))[0]
    others = [o for o in template_fields(db, t) if o.id != f.id]
    if row["type"] != "radio" and any(o.name == row["name"] for o in others):
        raise HTTPException(422, f"A field named '{row['name']}' already exists.")
    for k, v in row.items():
        setattr(f, k, v)
    t.version += 1
    db.commit()
    return field_out(f)


@router.delete("/templates/{template_id}/fields/{field_id}", status_code=204, summary="Remove one field")
def delete_field(template_id: str, field_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = get_owned_template(db, template_id, user)
    f = db.get(TemplateField, field_id)
    if not f or f.template_id != t.id:
        raise HTTPException(404, "Field not found")
    db.delete(f)
    t.version += 1
    db.commit()


# ---------------- integration helpers ----------------

@router.get("/templates/{template_id}/schema", summary="JSON Schema of the data this template accepts")
def template_schema(template_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Use it to validate your payloads, generate forms, or generate client code."""
    t = get_owned_template(db, template_id, user)
    roles = jl(t.roles, [])
    return {"data": data_schema(t.name, template_fields(db, t), roles),
            "roles": [{**r, "fields": sorted({f.name for f in template_fields(db, t) if f.role == r["id"]})} for r in roles]}


class CheckIn(BaseModel):
    data: Any = None
    field_map: dict[str, str] | None = None


@router.post("/templates/{template_id}/check", summary="Dry run: see how your data would be matched and validated")
def check_data(template_id: str, body: CheckIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = get_owned_template(db, template_id, user)
    fields = template_fields(db, t)
    try:
        raw, warnings = dn.resolve(body.data, fields, body.field_map)
    except FieldError as e:
        raise HTTPException(422, str(e))
    dn.derive_names(raw, fields)
    clean, errors = dn.coerce_values(raw, fields)
    unfilled = sorted({f.name for f in fields if f.name not in clean and f.type not in ("signature", "initials", "date_signed")})
    return {"values": clean, "errors": errors, "warnings": warnings, "unfilled_fields": unfilled, "valid": not errors}


@router.get("/templates/{template_id}/preview.png", summary="Render a page with sample data")
def preview(template_id: str, page: int = Query(1, ge=1), sample: bool = True,
            db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = get_owned_template(db, template_id, user)
    doc = db.get(Document, t.document_id)
    if page > doc.pages:
        raise HTTPException(404, "Page out of range")
    defaults, fields = jl(t.defaults, {}), template_fields(db, t)
    sig = typed_png("Aarav Sharma")
    ini = typed_png("AS")
    with _open(doc) as pdf:
        if sample:
            draw_fields(pdf, sample_fields(fields, defaults), font_resolver(db, user.id, defaults.get("font")),
                        lambda f: ini if f.type == "initials" else sig, FillReport())
        png = pdf[page - 1].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).tobytes("png")
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/templates/{template_id}/page/{page}.png", summary="The blank base page (used by the designer)")
def base_page(template_id: str, page: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = get_owned_template(db, template_id, user)
    doc = db.get(Document, t.document_id)
    if not 1 <= page <= doc.pages:
        raise HTTPException(404, "Page out of range")
    with _open(doc) as pdf:
        png = pdf[page - 1].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).tobytes("png")
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})
