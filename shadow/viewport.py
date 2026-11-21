"""Fixed viewport and the one transform from a model image back to it.

The live path captures 1280×720 at device scale 1. A crop, a resize, or both
are recorded on the image the model saw and applied once. Applying that
transform twice is a wrong coordinate, not a sharper one.
"""

from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageDraw, ImageFont


WIDTH, HEIGHT, SCALE = 1280, 720, 1


class ViewportError(ValueError):
    pass


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def open_png(blob: bytes) -> Image.Image:
    if not blob.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ViewportError("Screenshot is not a PNG.")
    image = Image.open(io.BytesIO(blob))
    image.load()
    return image.convert("RGB")


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


class Transform:
    """Map a point on the model image back to source pixels exactly once."""

    def __init__(self, crop: tuple[int, int, int, int] | None, model_size: tuple[int, int]):
        self.crop = crop
        self.model_size = model_size
        if crop is not None:
            left, top, right, bottom = crop
            if right <= left or bottom <= top:
                raise ViewportError("Crop is empty.")
            self._origin = (left, top)
            self._span = (right - left, bottom - top)
        else:
            self._origin = (0, 0)
            self._span = model_size

    def to_source(self, x: float, y: float) -> tuple[int, int]:
        width, height = self.model_size
        if width <= 0 or height <= 0:
            raise ViewportError("Model image has no size.")
        sx = self._origin[0] + x * self._span[0] / width
        sy = self._origin[1] + y * self._span[1] / height
        return round(sx), round(sy)


def crop_resize(image: Image.Image, crop: tuple[int, int, int, int], size: tuple[int, int]):
    """Return the model image and the single transform that maps it back."""
    view = image.crop(crop).resize(size, Image.Resampling.BOX)
    return view, Transform(crop, size)


def require_viewport(image: Image.Image) -> None:
    if image.size != (WIDTH, HEIGHT):
        raise ViewportError(f"Viewport is {image.size[0]}×{image.size[1]}, expected {WIDTH}×{HEIGHT}.")


def label(image: Image.Image, boxes: list[tuple[str, tuple[int, int, int, int]]]) -> bytes:
    """Numbered copy for Decision 1. The source image is left untouched."""
    marked = image.copy()
    draw = ImageDraw.Draw(marked)
    font = ImageFont.load_default(size=16)
    for key, (left, top, right, bottom) in boxes:
        draw.rectangle((left, top, right, bottom), outline="#ff2d2d", width=2)
        draw.text((left + 3, top + 2), key, font=font, fill="#111111",
                  stroke_width=1, stroke_fill="#ffffff")
    return png_bytes(marked)


def region_stable(before: Image.Image, after: Image.Image, box, tolerance=12) -> bool:
    left, top, right, bottom = box
    if before.size != after.size or right <= left or bottom <= top:
        return False
    first = before.crop(box).convert("L").tobytes()
    second = after.crop(box).convert("L").tobytes()
    if len(first) != len(second) or not first:
        return False
    drift = sum(abs(a - b) for a, b in zip(first, second)) / len(first)
    return drift <= tolerance
