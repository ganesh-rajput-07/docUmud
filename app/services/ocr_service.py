"""OCR with RapidOCR (ONNX, CPU, no system packages). Produces a searchable PDF by
adding an invisible text layer over each page, plus per-page extracted text."""
import statistics
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymupdf as fitz

from app.services.pdf_processor import ProcessingError

MIN_CONFIDENCE = 0.4
TARGET_ZOOM = 2.0   # ~144 dpi
MAX_SIDE_PX = 3200

_engine = None
_engine_lock = threading.Lock()


@dataclass
class PageText:
    text: str
    confidence: float


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR  # heavy import; load on first job
            _engine = RapidOCR()
        return _engine


def _reading_order(lines: list) -> list:
    """Sort (box, text, conf) lines top-to-bottom, left-to-right, grouping same-row lines."""
    if not lines:
        return lines
    ys = [(min(p[1] for p in b), max(p[1] for p in b)) for b, _, _ in lines]
    row_h = max(statistics.median(y1 - y0 for y0, y1 in ys) * 0.6, 1)
    return sorted(
        lines,
        key=lambda l: (round(((min(p[1] for p in l[0]) + max(p[1] for p in l[0])) / 2) / row_h),
                       min(p[0] for p in l[0])),
    )


def ocr_pdf(src: Path, dst: Path) -> tuple[list[PageText], dict]:
    try:
        engine = _get_engine()
    except Exception as exc:
        raise ProcessingError(f"OCR engine unavailable: {exc}") from exc

    pages: list[PageText] = []
    total_lines, conf_sum, skipped_rotated = 0, 0.0, 0
    with fitz.open(src) as pdf:
        for page in pdf:
            zoom = min(TARGET_ZOOM, MAX_SIDE_PX / max(page.rect.width, page.rect.height))
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
            img = np.ascontiguousarray(
                np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)[:, :, ::-1]
            )
            try:
                result, _ = engine(img)
            except Exception as exc:
                raise ProcessingError(f"OCR failed on page {page.number + 1}: {exc}") from exc
            lines = _reading_order([r for r in (result or []) if float(r[2]) >= MIN_CONFIDENCE])

            can_overlay = page.rotation == 0
            skipped_rotated += 0 if can_overlay else 1
            sx, sy = page.rect.width / pix.width, page.rect.height / pix.height
            for box, text, conf in lines:
                total_lines += 1
                conf_sum += float(conf)
                if not can_overlay:
                    continue
                x0, x1 = min(p[0] for p in box) * sx, max(p[0] for p in box) * sx
                y1 = max(p[1] for p in box) * sy
                safe = text.encode("latin-1", "replace").decode("latin-1")
                width_at_1pt = fitz.get_text_length(safe, fontname="helv", fontsize=1)
                if not safe.strip() or width_at_1pt <= 0:
                    continue
                size = min(max((x1 - x0) / width_at_1pt, 3), 72)
                # render_mode=3: invisible text, selectable and searchable.
                page.insert_text(fitz.Point(x0, y1 - size * 0.2), safe, fontname="helv",
                                 fontsize=size, render_mode=3)
            pages.append(PageText(
                text="\n".join(t for _, t, _ in lines),
                confidence=round(sum(float(c) for _, _, c in lines) / len(lines), 4) if lines else 0.0,
            ))
        pdf.save(dst, garbage=3, deflate=True)

    return pages, {
        "engine": "rapidocr-onnxruntime",
        "pages": len(pages),
        "lines": total_lines,
        "avg_confidence": round(conf_sum / total_lines, 4) if total_lines else 0.0,
        "skipped_rotated_pages": skipped_rotated,
        "note": "Latin characters only in the searchable layer; review important text manually.",
    }
