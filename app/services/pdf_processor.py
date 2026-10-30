"""Accessibility auto-tagging via OpenDataLoader (requires Java 11+)."""
import shutil
from pathlib import Path

from pypdf import PdfReader

from app.config import settings


class ProcessingError(RuntimeError):
    pass


def is_tagged(path: Path) -> bool:
    root = PdfReader(str(path)).trailer["/Root"]
    return "/StructTreeRoot" in root


def process_pdf(input_path: Path, job_id: str) -> tuple[Path, dict]:
    import opendataloader_pdf  # imported lazily: needs Java at call time

    if shutil.which("java") is None:
        raise ProcessingError("Java 11+ is required for PDF tagging but was not found.")

    out_dir = settings.storage_root / "tmp" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        opendataloader_pdf.convert(
            input_path=[str(input_path)],
            output_dir=str(out_dir),
            format="tagged-pdf",
            quiet=True,
            threads="1",
        )
    except Exception as exc:
        raise ProcessingError(f"Tagging engine failed: {exc}") from exc

    outputs = sorted(out_dir.rglob("*.pdf"))
    if not outputs:
        raise ProcessingError("Tagging engine produced no output.")
    output = outputs[0]
    report = {
        "engine": "opendataloader-pdf",
        "structure_tree_present": is_tagged(output),
        "pdfua_verified": False,
        "note": "Auto-tagged output. Not verified as PDF/UA compliant; human review required.",
    }
    return output, report
