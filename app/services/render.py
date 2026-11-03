"""Draw envelope/template fields onto a PDF: text in chosen fonts, check marks, radio dots, signatures."""
import json
from pathlib import Path
from typing import Callable, Iterable

import pymupdf

from app.services.fill_service import FillReport, Style, _draw
from app.services.field_types import BY_ID, display

DEFAULT_SIZE, DEFAULT_MULTILINE_SIZE = 14, 11
MULTILINE = {t.id for t in BY_ID.values() if t.multiline}
# If the chosen font has no glyph for a currency sign, print a readable substitute instead of a blank.
SYMBOL_FALLBACKS = {"₹": "Rs. ", "€": "EUR ", "£": "GBP ", "₨": "Rs. ", "¥": "JPY "}


def _printable(font: pymupdf.Font, text: str) -> str:
    return "".join(SYMBOL_FALLBACKS.get(ch, ch) if ch in SYMBOL_FALLBACKS and not font.has_glyph(ord(ch)) else ch for ch in text)


def _style(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _hex(color: str) -> tuple[float, float, float]:
    h = (color or "#111111").lstrip("#")
    try:
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (0.07, 0.07, 0.07)


def _rect(page: pymupdf.Page, f) -> pymupdf.Rect:
    r = page.rect
    return pymupdf.Rect(f.x * r.width, f.y * r.height, (f.x + f.w) * r.width, (f.y + f.h) * r.height)


def _pts(page: pymupdf.Page, rect: pymupdf.Rect, frac: list[tuple[float, float]]) -> list[pymupdf.Point]:
    """Points given as fractions of `rect` (displayed orientation) -> unrotated page space."""
    return [pymupdf.Point(rect.x0 + fx * rect.width, rect.y0 + fy * rect.height) * page.derotation_matrix for fx, fy in frac]


def _mark(page: pymupdf.Page, f, kind: str, color: str) -> None:
    rect = _rect(page, f)
    size = min(rect.width, rect.height)
    cx, cy = rect.x0 + rect.width / 2, rect.y0 + rect.height / 2
    rgb, width = _hex(color), max(size * 0.13, 0.8)
    if kind == "dot":
        c = pymupdf.Point(cx, cy) * page.derotation_matrix
        page.draw_circle(c, size * 0.28, color=rgb, fill=rgb)
    elif kind == "cross":
        for line in ([(0.2, 0.2), (0.8, 0.8)], [(0.8, 0.2), (0.2, 0.8)]):
            a, b = _pts(page, rect, line)
            page.draw_line(a, b, color=rgb, width=width)
    else:  # check
        page.draw_polyline(_pts(page, rect, [(0.18, 0.55), (0.42, 0.80), (0.85, 0.22)]), color=rgb, width=width, closePath=False)


def _image(page: pymupdf.Page, f, data: bytes) -> None:
    from PIL import Image
    import io

    with Image.open(io.BytesIO(data)) as im:
        ratio = im.width / im.height
    rect = _rect(page, f)
    w, h = rect.width, rect.width / ratio
    if h > rect.height:
        w, h = rect.height * ratio, rect.height
    fit = pymupdf.Rect(rect.x0 + (rect.width - w) / 2, rect.y0 + (rect.height - h) / 2, rect.x0 + (rect.width + w) / 2, rect.y0 + (rect.height + h) / 2)
    page.insert_image((fit * page.derotation_matrix).normalize(), stream=data, rotate=page.rotation)


def draw_fields(pdf: pymupdf.Document, fields: Iterable, font_for: Callable[[str | None], Path],
                image_for: Callable[[object], bytes | None], report: FillReport | None = None) -> FillReport:
    """Draw each field's value. `font_for(key)` returns a font file; `image_for(field)` returns signature PNG bytes."""
    report = report or FillReport()
    fonts: dict[Path, tuple[pymupdf.Font, str]] = {}
    for f in fields:
        if not 0 <= f.page - 1 < pdf.page_count:
            continue
        page, st = pdf[f.page - 1], _style(f.style)
        if f.type in ("signature", "initials"):
            data = image_for(f)
            if data:
                _image(page, f, data)
            continue
        if f.type == "checkbox":
            if f.value == "true":
                _mark(page, f, st.get("mark", "check"), st.get("color", "#111111"))
            continue
        if f.type == "radio":
            if f.value and f.value == f.option:
                _mark(page, f, st.get("mark", "dot"), st.get("color", "#111111"))
            continue
        text = display(f.type, f.value, st)
        if not text:
            continue
        path = font_for(st.get("font"))
        if path not in fonts:
            fonts[path] = (pymupdf.Font(fontfile=str(path)), f"DMF{len(fonts)}")
        font, fname = fonts[path]
        page.insert_font(fontname=fname, fontfile=str(path))
        multi = f.type in MULTILINE
        _draw(page, font, fname, f, _printable(font, text),
              Style(font_path=path, color=st.get("color", "#111111"), align=st.get("align", "left"),
                    max_font_size=int(st.get("font_size") or (DEFAULT_MULTILINE_SIZE if multi else DEFAULT_SIZE)),
                    valign="top" if multi else "middle"), report)
    return report


def finish(pdf: pymupdf.Document) -> bytes:
    try:
        pdf.subset_fonts()
    except Exception:
        pass
    return pdf.tobytes(garbage=3, deflate=True)
