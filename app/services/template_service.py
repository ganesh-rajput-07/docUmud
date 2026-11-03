"""Template definitions: validation, anchor placement, JSON Schema, sample data, style merging."""
import json
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pymupdf
from sqlalchemy.orm import Session

from app.services.field_types import BY_ID
from app.services.fonts import DEFAULT_FONT, plain_space_font, resolve_font
from app.services.storage import InvalidDocument

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DATE_FMT_RE = re.compile(r"^[%A-Za-z0-9 ,.\-/:]{1,30}$")
DEFAULT_ROLES = [
    {"id": "office", "label": "Office", "kind": "office", "order": 1},
    {"id": "signer", "label": "Signer", "kind": "signer", "order": 2},
]
STYLE_KEYS = {"font", "font_size", "color", "align", "uppercase", "date_format", "currency", "currency_position",
              "decimals", "grouping", "mark"}
VALIDATION_KEYS = {"pattern", "pattern_message", "min_length", "max_length", "min", "max"}


class DefinitionError(ValueError):
    pass


def jl(raw, default):
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, type(default)) else default
    except ValueError:
        return default


# ---------------- roles / style / validation ----------------

def clean_roles(roles: list[dict] | None) -> list[dict]:
    roles = roles or DEFAULT_ROLES
    seen, out, offices = set(), [], 0
    for i, r in enumerate(roles, 1):
        rid = str(r.get("id", "")).strip()
        if not ROLE_RE.match(rid):
            raise DefinitionError(f"Role id '{rid}' must be lowercase letters, digits or _ (starting with a letter).")
        if rid in seen:
            raise DefinitionError(f"Duplicate role '{rid}'.")
        seen.add(rid)
        kind = r.get("kind", "signer")
        if kind not in ("office", "signer"):
            raise DefinitionError(f"Role '{rid}': kind must be 'office' or 'signer'.")
        offices += kind == "office"
        out.append({"id": rid, "label": str(r.get("label") or rid.title())[:60], "kind": kind, "order": int(r.get("order") or i)})
    if offices > 1:
        raise DefinitionError("Only one role can be the office (the account owner).")
    return sorted(out, key=lambda r: r["order"])


def clean_style(style: dict | None, *, db: Session | None = None, user_id: str | None = None) -> dict:
    style = {k: v for k, v in (style or {}).items() if v not in (None, "")}
    unknown = set(style) - STYLE_KEYS
    if unknown:
        raise DefinitionError(f"Unknown style option(s): {', '.join(sorted(unknown))}. Allowed: {', '.join(sorted(STYLE_KEYS))}.")
    if "color" in style and not HEX_RE.match(str(style["color"])):
        raise DefinitionError("style.color must look like #1a2b3c.")
    if "align" in style and style["align"] not in ("left", "center", "right"):
        raise DefinitionError("style.align must be left, center or right.")
    if "font_size" in style:
        style["font_size"] = int(style["font_size"])
        if not 0 <= style["font_size"] <= 200:
            raise DefinitionError("style.font_size must be 0-200 (0 = fit the box).")
    if "decimals" in style and not 0 <= int(style["decimals"]) <= 6:
        raise DefinitionError("style.decimals must be 0-6.")
    if "grouping" in style and style["grouping"] not in ("intl", "indian", "none"):
        raise DefinitionError("style.grouping must be intl, indian or none.")
    if "currency_position" in style and style["currency_position"] not in ("prefix", "suffix"):
        raise DefinitionError("style.currency_position must be prefix or suffix.")
    if "mark" in style and style["mark"] not in ("check", "cross", "dot"):
        raise DefinitionError("style.mark must be check, cross or dot.")
    if "date_format" in style and not DATE_FMT_RE.match(str(style["date_format"])):
        raise DefinitionError("style.date_format must be a strftime pattern such as %d/%m/%Y.")
    if "currency" in style and len(str(style["currency"])) > 6:
        raise DefinitionError("style.currency is too long.")
    if "font" in style and db is not None:
        try:
            resolve_font(db, user_id, style["font"])
        except InvalidDocument:
            raise DefinitionError(f"Font '{style['font']}' is not available. Use GET /fonts/catalog for valid keys.")
    return style


def clean_validation(v: dict | None) -> dict:
    v = {k: x for k, x in (v or {}).items() if x not in (None, "")}
    unknown = set(v) - VALIDATION_KEYS
    if unknown:
        raise DefinitionError(f"Unknown validation option(s): {', '.join(sorted(unknown))}.")
    if "pattern" in v:
        try:
            re.compile(v["pattern"])
        except re.error as e:
            raise DefinitionError(f"validation.pattern is not a valid regular expression: {e}")
        if len(v["pattern"]) > 300:
            raise DefinitionError("validation.pattern is too long.")
    return v


# ---------------- anchors ----------------

def resolve_anchor(pdf: pymupdf.Document, anchor: dict, w: float, h: float) -> tuple[int, float, float]:
    """Find `anchor.text` in the PDF and return (page 1-based, x, y) for a w x h field placed relative to it."""
    text, occurrence = str(anchor.get("text", "")).strip(), max(int(anchor.get("occurrence") or 1), 1)
    if not text:
        raise DefinitionError("anchor.text is required.")
    pages = [int(anchor["page"]) - 1] if anchor.get("page") else range(pdf.page_count)
    seen = 0
    for pno in pages:
        if not 0 <= pno < pdf.page_count:
            raise DefinitionError(f"anchor.page {pno + 1} is outside the document.")
        page = pdf[pno]
        for hit in page.search_for(text):
            seen += 1
            if seen == occurrence:
                pw, ph = page.rect.width, page.rect.height
                pos = anchor.get("position", "right")
                gap_x, gap_y = 6 / pw, 3 / ph
                x0, y0, x1, y1 = hit.x0 / pw, hit.y0 / ph, hit.x1 / pw, hit.y1 / ph
                mid = (y0 + y1) / 2 - h / 2
                x, y = {"right": (x1 + gap_x, mid), "left": (x0 - gap_x - w, mid),
                        "below": (x0, y1 + gap_y), "over": (x0, y0)}.get(pos, (x1 + gap_x, mid))
                x += float(anchor.get("offset_x") or 0)
                y += float(anchor.get("offset_y") or 0)
                return pno + 1, min(max(x, 0.0), 1 - w), min(max(y, 0.0), 1 - h)
    raise DefinitionError(f"Anchor text '{text}' (occurrence {occurrence}) was not found in the document.")


# ---------------- field definitions ----------------

def auto_name(base: str, used: set[str]) -> str:
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", base).strip("_").lower() or "field"
    if not stem[0].isalpha():
        stem = "f_" + stem
    name, n = stem[:60], 2
    while name in used:
        name, n = f"{stem[:56]}_{n}", n + 1
    return name


def build_fields(defs: list[dict], roles: list[dict], *, pdf: pymupdf.Document | None, pages: int,
                 db: Session | None = None, user_id: str | None = None, start_position: int = 0) -> list[dict]:
    """Validate and normalise field definitions into rows for TemplateField."""
    role_ids = {r["id"] for r in roles}
    rows, used, radio_opts = [], set(), {}
    for i, d in enumerate(defs):
        where = f"fields[{i}]" + (f" ('{d.get('name')}')" if d.get("name") else "")
        try:
            ftype = d.get("type")
            ft = BY_ID.get(ftype)
            if ft is None:
                raise DefinitionError(f"unknown type '{ftype}'. Valid types: {', '.join(BY_ID)}.")
            role = d.get("role") or roles[0]["id"]
            if role not in role_ids:
                raise DefinitionError(f"role '{role}' is not defined on the template (roles: {', '.join(sorted(role_ids))}).")
            w = float(d["w"]) if d.get("w") else ft.w
            h = float(d["h"]) if d.get("h") else ft.h
            if d.get("anchor"):
                if pdf is None:
                    raise DefinitionError("anchors need the template's document.")
                page, x, y = resolve_anchor(pdf, d["anchor"], w, h)
            else:
                page, x, y = int(d.get("page") or 1), d.get("x"), d.get("y")
                if x is None or y is None:
                    raise DefinitionError("give x and y (fractions of the page) or an anchor.")
                x, y = float(x), float(y)
            if not 1 <= page <= pages:
                raise DefinitionError(f"page {page} is outside the document (it has {pages} pages).")
            if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.001 and y + h <= 1.001):
                raise DefinitionError("the field must lie within the page (x, y, w, h are fractions 0-1).")
            name = d.get("name") or auto_name(d.get("label") or ftype, used if ftype != "radio" else set())
            if not NAME_RE.match(name):
                raise DefinitionError(f"name '{name}' must start with a letter and use only letters, digits and _.")
            options = [str(o) for o in (d.get("options") or [])]
            option = str(d.get("option") or "")
            if ftype == "radio":
                if not option:
                    raise DefinitionError("radio fields need `option` (the choice this box represents).")
                if option in radio_opts.setdefault(name, []):
                    raise DefinitionError(f"radio group '{name}' already has an option '{option}'.")
                radio_opts[name].append(option)
            elif name in used:
                raise DefinitionError(f"duplicate field name '{name}'.")
            if ftype == "dropdown" and not options:
                raise DefinitionError("dropdown fields need `options`.")
            used.add(name)
            rows.append(dict(
                name=name, label=str(d.get("label") or "")[:120], type=ftype, role=role, page=page, x=x, y=y, w=w, h=h,
                required=bool(d.get("required", True)) and ftype not in ("date_signed",),
                default_value=str(d.get("default_value") or "")[:4000], options=json.dumps(options), option=option[:120],
                aliases=json.dumps([str(a) for a in (d.get("aliases") or [])][:20]),
                style=json.dumps(clean_style(d.get("style"), db=db, user_id=user_id)),
                validation=json.dumps(clean_validation(d.get("validation"))), position=start_position + i,
            ))
        except DefinitionError as e:
            raise DefinitionError(f"{where}: {e}")
    for r in rows:                                  # every radio box lists its group's choices
        if r["type"] == "radio":
            r["options"] = json.dumps(radio_opts[r["name"]])
    return rows


def effective_style(defaults: dict, style) -> dict:
    base = {k: defaults[k] for k in ("font", "font_size", "color", "align") if defaults.get(k) not in (None, "")}
    return {**base, **jl(style, {})}


def font_resolver(db: Session, user_id: str, default_key: str | None) -> Callable[[str | None], Path]:
    def font_for(key: str | None) -> Path:
        for candidate in (key, default_key, DEFAULT_FONT):
            if candidate:
                try:
                    return plain_space_font(resolve_font(db, user_id, candidate))
                except InvalidDocument:
                    continue
        raise InvalidDocument("No usable font.")
    return font_for


# ---------------- schema + samples ----------------

def _prop(f) -> dict:
    t = f.type
    opts = jl(f.options, [])
    p: dict = {"description": f.label or t.replace("_", " ").title(), "x-fieldType": t, "x-role": f.role}
    if t in ("number", "amount", "percentage"):
        p["type"] = ["number", "string"]
    elif t == "checkbox":
        p["type"] = ["boolean", "string"]
    elif t in ("date", "date_signed"):
        p.update(type="string", format="date", description=p["description"] + " (YYYY-MM-DD, DD/MM/YYYY, '5 Mar 2026'...)")
    elif t == "email":
        p.update(type="string", format="email")
    elif t in ("dropdown", "radio"):
        p.update(type="string", enum=opts)
    else:
        p["type"] = "string"
    v = jl(f.validation, {})
    if v.get("pattern"):
        p["pattern"] = v["pattern"]
    for src, dst in (("min_length", "minLength"), ("max_length", "maxLength"), ("min", "minimum"), ("max", "maximum")):
        if src in v:
            p[dst] = v[src]
    if f.default_value:
        p["default"] = f.default_value
    return p


def data_schema(template_name: str, fields: list, roles: list[dict]) -> dict:
    props, required, seen = {}, [], set()
    office = {r["id"] for r in roles if r["kind"] == "office"}
    for f in fields:
        if f.name in seen or f.type in ("signature", "initials", "date_signed"):
            continue
        seen.add(f.name)
        props[f.name] = _prop(f)
        if f.required and f.role in office and not f.default_value:
            required.append(f.name)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": f"{template_name} data", "type": "object",
            "properties": props, "required": required, "additionalProperties": True,
            "description": "Fields assigned to external signers may be omitted: the signer fills them in. Extra keys are ignored with a warning."}


def sample_value(f) -> str:
    t, opts = f.type, jl(f.options, [])
    return {"full_name": "Aarav Sharma", "first_name": "Aarav", "last_name": "Sharma", "email": "aarav@example.com",
            "phone": "+919876543210", "company": "Acme Pvt Ltd", "job_title": "Director",
            "address": "12 MG Road, Indiranagar\nBengaluru 560038", "text": "Sample text",
            "description": "This agreement sets out the terms between the parties. It continues for twelve months unless ended earlier.",
            "id_number": "ABCDE1234F", "number": "1250", "amount": "125000.5", "percentage": "12.5",
            "date": date.today().isoformat(), "date_signed": date.today().isoformat(), "checkbox": "true",
            "dropdown": opts[0] if opts else "Option", "radio": opts[0] if opts else f.option}.get(t, "")


def sample_fields(fields: list, defaults: dict) -> list:
    return [SimpleNamespace(**{c: getattr(f, c) for c in ("name", "type", "page", "x", "y", "w", "h", "option")},
                            value=sample_value(f), style=effective_style(defaults, f.style)) for f in fields]
