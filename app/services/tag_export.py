import json
from pathlib import Path

import pymupdf as fitz

from app.models import DocumentTag

STROKE = (0.31, 0.27, 0.9)


def write_tags(src: Path, dst: Path, tags: list[DocumentTag]) -> None:
    """Copy `src` to `dst` adding one rectangle annotation per tag, plus the full tag
    list as an embedded `documud-tags.json` attachment for machine consumers."""
    with fitz.open(src) as pdf:
        for tag in tags:
            if tag.page >= pdf.page_count:
                continue
            page = pdf[tag.page]
            r = page.rect
            rect = fitz.Rect(
                tag.x * r.width, tag.y * r.height,
                (tag.x + tag.w) * r.width, (tag.y + tag.h) * r.height,
            )
            # Annotation coordinates are in the unrotated page space.
            rect = (rect * page.derotation_matrix).normalize()
            annot = page.add_rect_annot(rect)
            annot.set_colors(stroke=STROKE)
            annot.set_border(width=1.2)
            annot.set_info(title=tag.label, content=tag.value or tag.label, subject=tag.label)
            annot.update()
        payload = [
            {"page": t.page, "x": t.x, "y": t.y, "w": t.w, "h": t.h, "label": t.label, "value": t.value}
            for t in tags
        ]
        pdf.embfile_add("documud-tags.json", json.dumps(payload, indent=2).encode())
        pdf.save(dst, garbage=3, deflate=True)
