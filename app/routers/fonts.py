import re
import uuid
from pathlib import Path

import pymupdf
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User, UserFont
from app.security import get_current_user
from app.services.access import read_upload
from app.services.fonts import BUNDLED
from app.services.storage import InvalidDocument, resolve

router = APIRouter(prefix="/fonts", tags=["Fonts"])

# TrueType (0x00010000, 'true') and OpenType/CFF ('OTTO') only.
_MAGIC = {b"\x00\x01\x00\x00": "ttf", b"true": "ttf", b"OTTO": "otf"}


def _out(f: UserFont) -> dict:
    return {"id": f.id, "name": f.name}


def _owned(db: Session, font_id: str, user: User) -> UserFont:
    font = db.get(UserFont, font_id)
    if not font or font.user_id != user.id:
        raise HTTPException(404, "Font not found")
    return font


@router.get("")
def list_fonts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    fonts = db.scalars(
        select(UserFont).where(UserFont.user_id == user.id).order_by(UserFont.created_at)
    ).all()
    return [_out(f) for f in fonts]


@router.get("/catalog")
def font_catalog(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Every font usable for filling documents: bundled (by category) and your own."""
    mine = db.scalars(select(UserFont).where(UserFont.user_id == user.id).order_by(UserFont.created_at)).all()
    return {
        "bundled": [{"key": f"bundled:{s}", "label": l, "category": c} for s, l, c in BUNDLED],
        "user": [{"key": f"user:{f.id}", "label": f.name} for f in mine],
    }


@router.post("", status_code=201)
def upload_font(
    file: UploadFile = File(...),
    name: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Upload a .ttf/.otf font to use for typed signatures."""
    count = db.scalar(select(func.count()).select_from(UserFont).where(UserFont.user_id == user.id))
    if count >= settings.max_user_fonts:
        raise HTTPException(409, f"You can add at most {settings.max_user_fonts} fonts.")
    data = read_upload(file, limit_mb=max(settings.max_font_kb // 1024, 1))
    if len(data) > settings.max_font_kb * 1024:
        raise HTTPException(413, f"Font exceeds {settings.max_font_kb} KB.")
    ext = _MAGIC.get(data[:4])
    if not ext:
        raise HTTPException(422, "Only TrueType (.ttf) and OpenType (.otf) fonts are supported.")
    try:
        pymupdf.Font(fontbuffer=data)  # parses the font; rejects corrupt files
    except Exception:
        raise HTTPException(422, "This font file could not be read.")

    label = re.sub(r"[^\w .\-]", "", name.strip() or Path(file.filename or "").stem)[:60].strip()
    rel = f"fonts/{uuid.uuid4()}.{ext}"
    resolve(rel).write_bytes(data)
    font = UserFont(user_id=user.id, name=label or "My font", file_path=rel)
    db.add(font)
    db.commit()
    return _out(font)


@router.get("/{font_id}/file")
def font_file(font_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    font = _owned(db, font_id, user)
    try:
        path = resolve(font.file_path)
    except InvalidDocument:
        raise HTTPException(404, "Font file unavailable")
    if not path.is_file():
        raise HTTPException(404, "Font file unavailable")
    return FileResponse(path, media_type="font/otf" if path.suffix == ".otf" else "font/ttf",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.delete("/{font_id}", status_code=204)
def delete_font(font_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    font = _owned(db, font_id, user)
    try:
        resolve(font.file_path).unlink(missing_ok=True)
    except InvalidDocument:
        pass
    db.delete(font)
    db.commit()
