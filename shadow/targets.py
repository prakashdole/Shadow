"""Find clickable rows and cards on the current screenshot.

Stable controls are the inbox column and the thread pane at 1280×720.
Moving rows and cards come from contrast bands on this image. Colored fixture
markers, DOM selectors and model-invented coordinates are not inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from PIL import Image


# ponytail: gutter scan at 1280×720. A fixed 360 split misses the wider dark-theme list.
OVERLAY = (40, 40, 1240, 680)
FALLBACK_SPLIT = 360


@dataclass(frozen=True)
class Candidate:
    key: str
    kind: str
    box: tuple[int, int, int, int]
    description: str
    expects: str

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.box
        return (left + right) // 2, (top + bottom) // 2


def _contrast_rows(image: Image.Image, box, *, gap=8, min_height=16, max_height=92, min_width=40):
    crop = image.convert("L").crop(box)
    width, height = crop.size
    pixels = crop.tobytes()
    rows = [pixels[y * width:(y + 1) * width] for y in range(height)]
    medians = [sorted(row)[width // 2] for row in rows]
    marked = [sum(abs(pixel - median) > 36 for pixel in row) >= max(14, width // 80)
              for row, median in zip(rows, medians)]
    bands, start = [], None
    for y, on in enumerate(marked + [False]):
        if on and start is None:
            start = y
        elif not on and start is not None:
            bands.append((start, y))
            start = None
    merged = []
    for band in bands:
        if merged and band[0] - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], band[1])
        else:
            merged.append(band)
    found, origin_x, origin_y = [], box[0], box[1]
    for top, bottom in merged:
        if not min_height <= bottom - top <= max_height:
            continue
        xs = sorted({x for y in range(top, bottom) for x, pixel in enumerate(rows[y])
                     if abs(pixel - medians[y]) > 36})
        if not xs:
            continue
        run_start = previous = xs[0]
        runs = []
        for x in xs[1:]:
            if x - previous > 24:
                runs.append((run_start, previous + 1))
                run_start = x
            previous = x
        runs.append((run_start, previous + 1))
        for left, right in runs:
            if right - left < min_width:
                continue
            found.append((origin_x + left, origin_y + top, origin_x + right, origin_y + bottom))
    return found


def _split(image: Image.Image) -> int | None:
    """Left edge of the gutter between the list and the conversation pane."""
    gray = image.convert("L")
    width, height = gray.size
    pixels = gray.tobytes()
    last = min(height, 680)

    def busy(x):
        column = [pixels[y * width + x] for y in range(80, last)]
        median = sorted(column)[len(column) // 2]
        return sum(abs(pixel - median) > 28 for pixel in column)

    start, stop = None, min(width, 640)
    for x in range(240, stop):
        if busy(x) < 16:
            start = x if start is None else start
            continue
        if start is not None and x - start >= 20 and _busy_before(busy, start):
            return start
        start = None
    if start is not None and stop - start >= 20 and _busy_before(busy, start):
        return start
    return None


def _busy_before(busy, start) -> bool:
    return any(busy(x) >= 16 for x in range(48, start, 4))


def thread_box(image: Image.Image) -> tuple[int, int, int, int]:
    """Conversation pane only. The list gutter is excluded so a transcript crop stays in one thread."""
    return _panels(image)[1]


def _panels(image: Image.Image):
    # A gutter left of 360 still uses 360 so a short label cannot flip the row median.
    split = _split(image)
    split = FALLBACK_SPLIT if split is None else max(split, FALLBACK_SPLIT)
    return (0, 56, split, 700), (split + 8, 64, 1260, 660)


def _list_rows(image: Image.Image, box):
    """Tall rows are message rows. Short bands are the fallback for text-only fixtures."""
    tall = _contrast_rows(image, box, min_height=36, max_height=70)
    return tall or _contrast_rows(image, box, min_height=16, max_height=70)


def _media_blocks(image: Image.Image, box):
    """Portrait or square cards. Taller than a text bubble, so the default band limit drops them."""
    found = _contrast_rows(image, box, min_height=80, max_height=520, min_width=70)
    return [block for block in found if block[3] - block[1] >= 120]


def _blank(image: Image.Image) -> bool:
    pixels = image.convert("L").tobytes()
    sample = pixels[::16]
    mean = sum(sample) / len(sample)
    variance = sum((pixel - mean) ** 2 for pixel in sample) / len(sample)
    return variance < 40


def classify(image: Image.Image, url: str = "") -> str:
    """Screen class from this image. A thread URL with too little text is still loading."""
    if any(part in url for part in ("/accounts/login", "/challenge/", "checkpoint_required")):
        return "login"
    if _blank(image):
        return "blank"
    if _split(image) is None and _dark_center(image):
        return "overlay"
    inbox, thread = _panels(image)
    # A story tray is taller than a message row. A dark theme with a list is not a modal.
    rows = _list_rows(image, inbox)
    if len(rows) < 2 and _dark_center(image) and _contrast_rows(image, OVERLAY, max_height=70):
        return "overlay"
    if urlsplit(url).path.rstrip("/") == "/direct/inbox":
        return "inbox" if rows else "header_only"
    thread_bands = _contrast_rows(image, thread)
    if len(thread_bands) >= 3 or _media_blocks(image, thread):
        return "thread"
    if "/direct/t/" in url:
        return "header_only"
    if rows:
        return "inbox"
    if thread_bands:
        return "header_only"
    return "unknown"


def _dark_center(image: Image.Image) -> bool:
    gray = image.convert("L")
    center = gray.crop((int(image.width * 0.15), int(image.height * 0.12),
                        int(image.width * 0.85), int(image.height * 0.88))).tobytes()
    return sum(center) / len(center) < 80


def _top_right_close(image: Image.Image):
    """Dim close mark on a dark player header. Flat headers produce no candidate."""
    gray = image.convert("L")
    values = []
    for x in range(1080, min(gray.width, 1264)):
        column = gray.crop((x, 46, x + 1, 74)).tobytes()
        values.append(sum(column) / len(column))
    if not values:
        return None
    peak = max(values)
    if peak < sorted(values)[len(values) // 2] + 4:
        return None
    x = 1080 + values.index(peak)
    return (max(0, x - 10), 46, min(gray.width, x + 10), 74)


def offer(image: Image.Image, screen: str) -> list[Candidate]:
    """Actions that are legal on this screen. Empty when a click would be a guess."""
    if screen in ("blank", "header_only", "login", "unknown"):
        return []
    inbox, thread = _panels(image)
    if screen == "overlay":
        bands = _contrast_rows(image, OVERLAY, max_height=70)
        controls = [Candidate(f"control-{index}", "close", box,
                              f"Overlay control {index}, counted from the top.", "thread")
                    for index, box in enumerate(bands, start=1)]
        peak = _top_right_close(image)
        if peak:
            controls.append(Candidate("close-corner", "close", peak,
                                      "Bright mark in the top-right of the player.", "thread"))
        return controls
    if screen == "inbox":
        return [Candidate(f"row-{index}", "row", box,
                          f"Inbox row {index}, counted from the top.", "thread")
                for index, box in enumerate(_list_rows(image, inbox), start=1)]
    if screen == "thread":
        cards = [Candidate(f"card-{index}", "card", box,
                           f"Thread card or control {index}, counted from the top.", "overlay")
                 for index, box in enumerate(_contrast_rows(image, thread), start=1)]
        cards += [Candidate(f"media-{index}", "card", box,
                            f"Shared media card {index}, counted from the largest.", "overlay")
                  for index, box in enumerate(_media_blocks(image, thread), start=1)]
        return cards + [Candidate("scroll", "scroll", (560, 150, 900, 250),
                                  "Scroll the open conversation.", "thread")]
    return []
