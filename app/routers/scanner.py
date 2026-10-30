import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User
from app.routers.documents import doc_out
from app.security import get_current_user
from app.services.access import read_upload
from app.services.scanner_service import images_to_pdf
from app.services.storage import InvalidDocument, new_path, register_pdf

router = APIRouter(prefix="/scanner", tags=["Scanner"])

MAX_PAGES = 20


@router.post("/convert", status_code=201)
def convert_scans(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Combine ordered page images (camera captures) into one PDF."""
    if not 1 <= len(files) <= MAX_PAGES:
        raise HTTPException(400, f"Provide between 1 and {MAX_PAGES} pages.")
    out, rel = new_path("scans")
    with tempfile.TemporaryDirectory(dir=settings.storage_root / "tmp") as tmp:
        paths = []
        for i, f in enumerate(files):
            p = Path(tmp) / f"{i}.img"
            p.write_bytes(read_upload(f))
            paths.append(p)
        try:
            images_to_pdf(paths, out)
            doc = register_pdf(
                db, user_id=user.id, path=out, relative=rel,
                filename="scan.pdf", source="scanned",
            )
        except InvalidDocument as e:
            out.unlink(missing_ok=True)
            raise HTTPException(422, str(e))
    db.commit()
    return doc_out(doc)
