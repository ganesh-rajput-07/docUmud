"""Server-side signature images (typed signatures for previews and the office's saved signature)."""
import io

from PIL import Image, ImageDraw, ImageFont

from app.services.fonts import bundled_path


def typed_png(text: str, font_stem: str = "GreatVibes", color: str = "#111111", height: int = 200) -> bytes:
    """A transparent PNG of `text` in a bundled script font, cropped to the ink."""
    path = bundled_path(font_stem) or bundled_path("GreatVibes")
    font = ImageFont.truetype(str(path), int(height * 0.62))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
    pad = 12
    img = Image.new("RGBA", (right - left + 2 * pad, bottom - top + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - left, pad - top), text, font=font, fill=color)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def validate_png(data: bytes, max_bytes: int = 1_000_000) -> None:
    from app.services.storage import InvalidDocument

    if len(data) > max_bytes:
        raise InvalidDocument("Signature image is too large.")
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format != "PNG":
                raise InvalidDocument("Signature must be a PNG image.")
            if img.width * img.height > 4_000_000:
                raise InvalidDocument("Signature image resolution is too large.")
    except InvalidDocument:
        raise
    except Exception:
        raise InvalidDocument("Signature image is invalid.")
