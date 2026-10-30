"""Font catalog: bundled OFL fonts + per-user uploads, addressed by a string key."""
import hashlib
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings

from app.models import UserFont
from app.services.storage import InvalidDocument, resolve

STATIC_FONTS = Path(__file__).resolve().parent.parent / "static" / "fonts"

# (file stem, label, category)
BUNDLED = [
    ("Lato", "Lato", "Clean"),
    ("OpenSans", "Open Sans", "Clean"),
    ("RobotoMono", "Roboto Mono", "Clean"),
    ("LibreBaskerville", "Libre Baskerville", "Serif"),
    ("Caveat", "Caveat", "Handwriting"),
    ("Kristi", "Kristi", "Handwriting"),
    ("MarckScript", "Marck Script", "Handwriting"),
    ("GreatVibes", "Great Vibes", "Signature"),
    ("Allura", "Allura", "Signature"),
    ("Sacramento", "Sacramento", "Signature"),
    ("MrDafoe", "Mr Dafoe", "Signature"),
    ("HerrVonMuellerhoff", "Herr Von Muellerhoff", "Signature"),
    ("DancingScript", "Dancing Script", "Signature"),
]
DEFAULT_FONT = "bundled:Lato"


def bundled_path(stem: str) -> Path | None:
    return next((STATIC_FONTS / f"{s}.ttf" for s, _, _ in BUNDLED if s == stem), None)


def resolve_font(db: Session, user_id: str, key: str) -> Path:
    """Map a font key to a file the user may use. Raises InvalidDocument if unknown."""
    kind, _, name = key.partition(":")
    if kind == "bundled":
        path = bundled_path(name)
        if path and path.is_file():
            return path
    elif kind == "user":
        font = db.get(UserFont, name)
        if font and font.user_id == user_id:
            path = resolve(font.file_path)
            if path.is_file():
                return path
    raise InvalidDocument("Selected font is not available.")


logger = logging.getLogger("documud.fonts")


def plain_space_font(path: Path) -> Path:
    """Return a font file whose U+00A0 isn't mapped to the same glyph as a normal space.

    Fonts like Lato map both to one glyph, so text drawn with them is extracted from the PDF
    with non-breaking spaces, which hurts search, copy/paste and screen readers. Fixed copies
    are cached; on any problem the original path is returned.
    """
    try:
        key = hashlib.sha1(f"{path}:{path.stat().st_mtime_ns}".encode()).hexdigest()
        cached = settings.storage_root / "fonts" / ".cache" / f"{key}{path.suffix}"
        if cached.is_file():
            return cached
        from fontTools.ttLib import TTFont

        font = TTFont(str(path))
        changed = False
        for table in font["cmap"].tables:
            cmap = table.cmap
            if 0xA0 in cmap and cmap.get(0x20) == cmap[0xA0]:
                del cmap[0xA0]
                changed = True
        if not changed:
            return path
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(".part")
        font.save(str(tmp))
        tmp.replace(cached)
        return cached
    except Exception:
        logger.warning("Could not normalise spaces in font %s", path, exc_info=True)
        return path
