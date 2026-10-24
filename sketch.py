"""Cut the aircraft out of a photograph and redraw it by hand, as it were.

The cut-out uses rembg (a small segmentation model run locally with
onnxruntime). The drawing is pure Pillow, in one of two styles:
  - watercolour: flattened pigment, darker edges, a wet tide-line round the
    silhouette and a faint pencil outline
  - ink: an outline, fine detail lines, and hatching that follows the tone
Each airframe is drawn once and cached, so the cost is only paid the first
time it is seen.
"""

import math
import random
import threading
from collections import deque

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

WIDTH = 720
INK = (38, 33, 29)
PAPER = (250, 246, 236)
MODEL = "isnet-general-use"

_session = None
_session_lock = threading.Lock()


def _rembg_session():
    global _session
    with _session_lock:
        if _session is None:
            from rembg import new_session

            _session = new_session(MODEL)
        return _session


def _keep_main_shape(alpha: Image.Image) -> Image.Image:
    """Drop specks of background the model let through: keep the biggest blobs."""
    small = alpha.resize((max(1, alpha.width // 4), max(1, alpha.height // 4)))
    w, h = small.size
    px = small.load()
    label = [[0] * w for _ in range(h)]
    sizes = {}
    n = 0
    for y in range(h):
        for x in range(w):
            if px[x, y] > 60 and not label[y][x]:
                n += 1
                label[y][x] = n
                queue, count = deque([(x, y)]), 0
                while queue:
                    cx, cy = queue.popleft()
                    count += 1
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < w and 0 <= ny < h and not label[ny][nx] and px[nx, ny] > 60:
                            label[ny][nx] = n
                            queue.append((nx, ny))
                sizes[n] = count
    if not sizes:
        return alpha
    biggest = max(sizes.values())
    keep = {k for k, v in sizes.items() if v >= biggest * 0.12}
    keep_mask = Image.new("L", (w, h), 0)
    kp = keep_mask.load()
    for y in range(h):
        for x in range(w):
            if label[y][x] in keep:
                kp[x, y] = 255
    keep_mask = keep_mask.filter(ImageFilter.MaxFilter(5)).resize(alpha.size)
    return ImageChops.multiply(alpha, keep_mask)


def cutout(photo: Image.Image) -> Image.Image:
    """The aircraft alone on a transparent background, cropped tight."""
    from rembg import remove

    rgba = remove(photo.convert("RGB"), session=_rembg_session())
    alpha = _keep_main_shape(rgba.getchannel("A"))
    rgba.putalpha(alpha)
    box = alpha.point(lambda v: 255 if v > 40 else 0).getbbox()
    if box:
        pad = 6
        box = (max(0, box[0] - pad), max(0, box[1] - pad),
               min(rgba.width, box[2] + pad), min(rgba.height, box[3] + pad))
        rgba = rgba.crop(box)
    return rgba


def _hatch(size, angle_deg, spacing, rng) -> Image.Image:
    """White sheet covered in slightly wobbly, broken parallel pen strokes."""
    w, h = size
    sheet = Image.new("L", size, 255)
    draw = ImageDraw.Draw(sheet)
    a = math.radians(angle_deg)
    ux, uy = math.cos(a), math.sin(a)       # along the stroke
    nx, ny = -uy, ux                        # across the strokes
    reach = math.hypot(w, h)
    cx, cy = w / 2, h / 2
    off = -reach
    while off < reach:
        o = off + rng.uniform(-spacing * 0.25, spacing * 0.25)
        t = -reach
        while t < reach:
            seg = rng.uniform(25, 90)
            wobble = rng.uniform(-0.6, 0.6)
            x0, y0 = cx + nx * o + ux * t, cy + ny * o + uy * t
            x1, y1 = cx + nx * (o + wobble) + ux * (t + seg), cy + ny * (o + wobble) + uy * (t + seg)
            draw.line([(x0, y0), (x1, y1)], fill=0, width=1)
            t += seg + rng.uniform(2, 7)    # pen lifts between strokes
        off += spacing
    return sheet


def _where(tone: Image.Image, below: int, noise: Image.Image) -> Image.Image:
    """Mask (255 = apply) of areas darker than `below`, with a ragged boundary."""
    jittered = ImageChops.add(tone, noise, scale=1.0, offset=-128)
    return jittered.point(lambda v: 255 if v < below else 0)


def ink_sketch(cut: Image.Image, seed: str = "") -> Image.Image:
    rng = random.Random(seed)
    scale = WIDTH / cut.width
    cut = cut.resize((WIDTH, max(1, round(cut.height * scale))), Image.LANCZOS)
    size = cut.size
    alpha = cut.getchannel("A")

    # Grey version on white, so the background reads as blank paper.
    white = Image.new("RGB", size, (255, 255, 255))
    gray = Image.composite(cut.convert("RGB"), white, alpha).convert("L")
    gray = ImageOps.autocontrast(gray, cutoff=1)

    # Fine detail lines wherever a pixel is darker than its surroundings
    # (panel lines, windows, livery edges).
    darker_than_nearby = ImageChops.subtract(gray.filter(ImageFilter.GaussianBlur(3)), gray)
    lines = darker_than_nearby.point(lambda v: 0 if v > 28 else (255 if v < 10 else 255 - (v - 10) * 14))

    # Bold outline round the silhouette.
    solid = alpha.point(lambda v: 255 if v > 110 else 0)
    outline = ImageChops.subtract(solid.filter(ImageFilter.MaxFilter(3)), solid.filter(ImageFilter.MinFilter(3)))
    outline = ImageOps.invert(outline)

    # Hatching: more layers of strokes the darker the tone.
    tone = gray.filter(ImageFilter.GaussianBlur(2.5))
    noise = Image.effect_noise(size, 30).filter(ImageFilter.GaussianBlur(3))
    paper = Image.new("L", size, 255)
    shading = paper
    for angle, spacing, below in ((-38, 6, 175), (52, 6, 120), (-38, 3, 70), (8, 4, 45)):
        strokes = _hatch(size, angle + rng.uniform(-4, 4), spacing, rng)
        shading = Image.composite(ImageChops.darker(shading, strokes), shading, _where(tone, below, noise))

    ink = ImageChops.darker(ImageChops.darker(lines, outline), shading)

    # Output: ink colour, opaque where there's ink, only inside the aircraft.
    coverage = ImageChops.multiply(ImageOps.invert(ink), alpha.point(lambda v: 255 if v > 50 else int(v * 5)))
    coverage = coverage.filter(ImageFilter.GaussianBlur(0.35))
    out = Image.new("RGBA", size, INK + (0,))
    out.putalpha(coverage)
    return out


def _blotches(size: tuple[int, int], cell: int, sigma: float) -> Image.Image:
    """Smooth low-frequency noise centred on 128."""
    w, h = size
    small = Image.effect_noise((max(2, w // cell), max(2, h // cell)), sigma)
    return small.resize(size, Image.BICUBIC).filter(ImageFilter.GaussianBlur(cell / 4))


def soft_watercolour(cut: Image.Image, seed: str = "") -> Image.Image:
    """Loose and wet: soft pigment patches, colours bleeding, a wet edge."""
    cut = cut.resize((WIDTH, max(1, round(cut.height * WIDTH / cut.width))), Image.LANCZOS)
    size = cut.size
    alpha = cut.getchannel("A")
    img = Image.composite(cut.convert("RGB"), Image.new("RGB", size, PAPER), alpha)

    # Flatten detail into pigment patches.
    paint = img.filter(ImageFilter.MedianFilter(5)).filter(ImageFilter.ModeFilter(7))
    paint = ImageOps.posterize(paint, 4).filter(ImageFilter.GaussianBlur(1.6))
    # Wet-in-wet: colours bleed into each other in places.
    bleed = paint.filter(ImageFilter.GaussianBlur(6))
    paint = Image.composite(bleed, paint, _blotches(size, 60, 90).point(lambda v: max(0, v - 90) * 2))
    paint = ImageEnhance.Color(paint).enhance(0.9)
    # Watercolour is translucent: lift everything towards the paper.
    paint = Image.blend(paint, Image.new("RGB", size, PAPER), 0.22)

    # Pigment gathers at the edges of shapes.
    edges = img.convert("L").filter(ImageFilter.GaussianBlur(1.5)).filter(ImageFilter.FIND_EDGES)
    edges = ImageOps.autocontrast(edges, cutoff=2).filter(ImageFilter.GaussianBlur(1))
    paint = ImageChops.multiply(paint, ImageOps.invert(edges).point(lambda v: 255 - (255 - v) * 0.35).convert("RGB"))

    # Uneven wash density and fine paper grain.
    density = _blotches(size, 30, 40).point(lambda v: max(200, min(255, 228 + (v - 128) // 2)))
    grain = Image.effect_noise(size, 14).filter(ImageFilter.GaussianBlur(0.7)).point(lambda v: min(255, 236 + (v - 128) // 3))
    paint = ImageChops.multiply(paint, ImageChops.multiply(density, grain).point(lambda v: min(255, v + 30)).convert("RGB"))

    # Wet edge: the silhouette softened and made slightly irregular...
    soft = alpha.filter(ImageFilter.GaussianBlur(2))
    wobble = _blotches(size, 14, 50)
    wet = ImageChops.add(soft, wobble, scale=1.0, offset=-128).point(lambda v: 0 if v < 70 else min(255, (v - 70) * 2))
    core = alpha.filter(ImageFilter.MinFilter(5))
    # Irregularity only eats inward, so no pale fringe spreads beyond the aircraft.
    mask = ImageChops.darker(ImageChops.lighter(wet, core), alpha.filter(ImageFilter.GaussianBlur(0.8)))
    # ...with a darker tide-line where the pigment dried at the edge.
    tideline = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(5))).filter(ImageFilter.GaussianBlur(1))
    paint = ImageChops.multiply(paint, ImageOps.invert(tideline).point(lambda v: 255 - (255 - v) * 0.4).convert("RGB"))

    # A faint, slightly offset pencil outline, as if sketched before painting.
    solid = alpha.point(lambda v: 255 if v > 110 else 0)
    line = ImageChops.subtract(solid.filter(ImageFilter.MaxFilter(3)), solid)
    line = ImageChops.offset(line, 1, -1).point(lambda v: v * 0.3)

    out = paint.convert("RGBA")
    out.putalpha(mask)
    pencil = Image.new("RGBA", size, INK + (0,))
    pencil.putalpha(line.convert("L"))
    out.alpha_composite(pencil)
    return out


def watercolour(cut: Image.Image, seed: str = "") -> Image.Image:
    """Pen and wash: clean, defined areas of colour held together by fine ink lines."""
    cut = cut.resize((WIDTH, max(1, round(cut.height * WIDTH / cut.width))), Image.LANCZOS)
    size = cut.size
    alpha = cut.getchannel("A")
    img = Image.composite(cut.convert("RGB"), Image.new("RGB", size, PAPER), alpha)

    # Flat areas of colour that keep their shapes: light smoothing, no blur.
    paint = img.filter(ImageFilter.MedianFilter(3))
    paint = ImageOps.posterize(paint, 5).filter(ImageFilter.ModeFilter(3))
    # Only a hint of colour bleeding, in a few places.
    bleed = paint.filter(ImageFilter.GaussianBlur(2.5))
    paint = Image.composite(bleed, paint, _blotches(size, 50, 90).point(lambda v: max(0, v - 120) * 3))
    paint = ImageEnhance.Color(paint).enhance(1.05)
    paint = Image.blend(paint, Image.new("RGB", size, PAPER), 0.12)
    paint = paint.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))

    # Pigment pooling along colour boundaries, kept crisp.
    edges = img.convert("L").filter(ImageFilter.GaussianBlur(0.8)).filter(ImageFilter.FIND_EDGES)
    edges = ImageOps.autocontrast(edges, cutoff=2)
    paint = ImageChops.multiply(paint, ImageOps.invert(edges).point(lambda v: 255 - (255 - v) * 0.4).convert("RGB"))

    # Gentle wash density and paper grain.
    density = _blotches(size, 30, 30).point(lambda v: max(215, min(255, 236 + (v - 128) // 3)))
    grain = Image.effect_noise(size, 12).filter(ImageFilter.GaussianBlur(0.6)).point(lambda v: min(255, 240 + (v - 128) // 4))
    paint = ImageChops.multiply(paint, ImageChops.multiply(density, grain).point(lambda v: min(255, v + 22)).convert("RGB"))

    # Ink line work: fine detail (panel lines, windows, livery edges)...
    gray = ImageOps.autocontrast(img.convert("L"), cutoff=1)
    detail = ImageChops.subtract(gray.filter(ImageFilter.GaussianBlur(2)), gray)
    detail = detail.point(lambda v: 0 if v < 8 else min(255, (v - 8) * 12)).point(lambda v: int(v * 0.55))
    # ...and a crisp outline round the silhouette.
    solid = alpha.point(lambda v: 255 if v > 110 else 0)
    outline = ImageChops.subtract(solid, solid.filter(ImageFilter.MinFilter(3))).point(lambda v: int(v * 0.8))
    lines = ImageChops.lighter(detail, outline).filter(ImageFilter.GaussianBlur(0.3))

    mask = alpha.filter(ImageFilter.GaussianBlur(0.6))
    out = paint.convert("RGBA")
    out.putalpha(mask)
    ink = Image.new("RGBA", size, INK + (0,))
    ink.putalpha(ImageChops.multiply(lines, mask))
    out.alpha_composite(ink)
    return out


STYLES = {"watercolour": watercolour, "soft": soft_watercolour, "ink": ink_sketch}
