from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.services.storage import InvalidDocument

MAX_PIXELS = 40_000_000


def images_to_pdf(image_paths: list[Path], output_path: Path) -> None:
    if not image_paths:
        raise InvalidDocument("No images supplied.")
    pages: list[Image.Image] = []
    try:
        for p in image_paths:
            with Image.open(p) as img:
                if img.width * img.height > MAX_PIXELS:
                    raise InvalidDocument("Image resolution is too large.")
                img = ImageOps.exif_transpose(img).convert("RGB")
                pages.append(img.copy())
    except UnidentifiedImageError:
        raise InvalidDocument("One of the files is not a valid image.")
    first, *rest = pages
    first.save(output_path, format="PDF", save_all=True, append_images=rest, resolution=150)
    for page in pages:
        page.close()
