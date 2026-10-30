"""Write dataset values into tagged regions of a PDF, in a chosen font."""
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from app.models import DocumentTag

MIN_SIZE = 5.0
PAD = 2.0


@dataclass
class Style:
    font_path: Path
    color: str = "#111111"
    align: str = "left"          # left | center | right
    max_font_size: int = 0       # 0 = limited only by box height


@dataclass
class FillReport:
    overflow: int = 0            # cells that had to be truncated
    warnings: list[str] = field(default_factory=list)


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _draw(page: pymupdf.Page, font: pymupdf.Font, fontname: str, tag: DocumentTag,
          text: str, style: Style, report: FillReport) -> None:
    r = page.rect                                          # displayed (rotated) page size
    # Layout happens in displayed orientation; w, h = tag box minus padding.
    w, h = tag.w * r.width - 2 * PAD, tag.h * r.height - 2 * PAD
    if w <= 0 or h <= 0 or not text:
        return
    top = min(h * 0.85, style.max_font_size or 1e9)
    asc, desc = font.ascender, font.descender          # em fractions; descender is negative
    line_h = lambda size: (asc - desc) * size

    # 1) single line: largest size that fits width and height
    size = top
    while size > MIN_SIZE and (font.text_length(text, size) > w or line_h(size) > h):
        size -= 0.5
    lines = [text]
    if font.text_length(text, size) > w or line_h(size) > h:
        # 2) wrap onto several lines, shrinking until it fits the box
        size = min(top, h / 1.2)
        while True:
            lines = _wrap(font, text, size, w)
            if len(lines) * line_h(size) * 1.05 <= h or size <= MIN_SIZE:
                break
            size -= 0.5
        max_lines = max(int(h // (line_h(size) * 1.05)), 1)
        if len(lines) > max_lines:                      # still too long at the minimum size
            report.overflow += 1
            lines = lines[:max_lines]
            lines[-1] = _ellipsize(font, lines[-1], size, w)

    block_h = line_h(size) * (1 if len(lines) == 1 else 1.05 * len(lines))
    y = PAD + (h - block_h) / 2 + asc * size              # baseline of first line, displayed coords
    for ln in lines:
        tl = font.text_length(ln, size)
        x = PAD + {"left": 0, "center": (w - tl) / 2, "right": w - tl}[style.align]
        _put(page, tag.x * r.width + x, tag.y * r.height + y, ln, fontname, size, style.color)
        y += line_h(size) * 1.05


def _put(page: pymupdf.Page, x: float, y: float, text: str, fontname: str, size: float, color: str) -> None:
    """Draw text at a baseline point given in displayed coordinates; maps to unrotated page space."""
    pt = pymupdf.Point(x, y) * page.derotation_matrix
    page.insert_text(pt, text, fontsize=size, fontname=fontname, color=_rgb(color), rotate=page.rotation)


def _wrap(font: pymupdf.Font, text: str, size: float, width: float) -> list[str]:
    lines, cur = [], ""
    for word in re.split(r"(\s+)", text):
        trial = cur + word
        if font.text_length(trial.rstrip(), size) <= width or not cur.strip():
            cur = trial
        else:
            lines.append(cur.rstrip())
            cur = word.lstrip()
    if cur.strip():
        lines.append(cur.rstrip())
    return lines or [""]


def _ellipsize(font: pymupdf.Font, text: str, size: float, width: float) -> str:
    while text and font.text_length(text + "…", size) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def fill_document(src: Path, tags: list[DocumentTag], values: list[str | None], style: Style,
                  report: FillReport, doc: pymupdf.Document | None = None) -> pymupdf.Document:
    """Return a filled copy of `src`; `values[i]` is the text for `tags[i]` (None/'' = skip)."""
    pdf = doc or pymupdf.open(src)
    font = pymupdf.Font(fontfile=str(style.font_path))
    fontname = "DMFill"
    for page_no in sorted({t.page for t in tags}):
        if page_no >= pdf.page_count:
            continue
        page = pdf[page_no]
        page.insert_font(fontname=fontname, fontfile=str(style.font_path))   # reused if already in the document
        for tag, text in zip(tags, values):
            if tag.page == page_no and text:
                _draw(page, font, fontname, tag, text, style, report)
    return pdf


def finish(pdf: pymupdf.Document) -> bytes:
    try:
        pdf.subset_fonts()          # keeps files small; needs fontTools
    except Exception:
        pass
    return pdf.tobytes(garbage=3, deflate=True)


def safe_stem(value: str, fallback: str) -> str:
    stem = re.sub(r"[^\w.\- ]", "_", value).strip(" ._")[:80]
    return stem or fallback


def build_zip(files: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:   # PDFs are already compressed
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()
