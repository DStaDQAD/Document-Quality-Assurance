"""EMF pictures drawn to PNG with Pillow, so a chart pasted from Excel into a Word report can be shown
to a vision model on a Linux server (Windows draws EMF itself; Pillow on Linux cannot, and LibreOffice
is too heavy for Render's 512 MB).

Enough of GDI for Excel's charts: the page and world transforms, pens and brushes, lines, polylines,
polygons, beziers and paths, rectangles and ellipses, text at its bounds, and bitmaps with the raster
operations Excel uses for its markers. EMF+ records (in GDI comments) are skipped: an Excel EMF is
"dual", its GDI records draw the same picture. Clipping is ignored. Checked against Windows' own
rendering of the SKDU and NPI charts with tools/emf_render_compare.py.
"""
import io
import logging
import math
import struct
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFont

logger = logging.getLogger("fact-checker")

_EMR_HEADER, _EMR_EOF = 1, 14
_EMF_SIGNATURE = b" EMF"
_BEZIER_STEPS = 16
_WHITE = (255, 255, 255)

# Stock objects (SELECTOBJECT with the high bit set).
_STOCK_BRUSHES = {0: (255, 255, 255), 1: (192, 192, 192), 2: (128, 128, 128), 3: (64, 64, 64), 4: (0, 0, 0), 5: None}
_STOCK_PENS = {6: ((255, 255, 255), 1.0), 7: ((0, 0, 0), 1.0), 8: None}

# Raster operations.
_SRCCOPY, _SRCPAINT, _SRCAND, _PATCOPY, _PATINVERT, _NOP = 0xCC0020, 0xEE0086, 0x8800C6, 0xF00021, 0x5A0049, 0xAA0029
_WHITENESS, _BLACKNESS = 0xFF0062, 0x000042

Point = Tuple[float, float]
Matrix = Tuple[float, float, float, float, float, float]   # M11 M12 M21 M22 Dx Dy, point · M
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _colour(ref: int) -> Tuple[int, int, int]:
    return ref & 0xFF, (ref >> 8) & 0xFF, (ref >> 16) & 0xFF


def _multiply(a: Matrix, b: Matrix) -> Matrix:
    """a then b (row-vector convention: p · a · b)."""
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


@dataclass
class _State:
    world: Matrix = _IDENTITY
    window_org: Point = (0.0, 0.0)
    window_ext: Point = (1.0, 1.0)
    viewport_org: Point = (0.0, 0.0)
    viewport_ext: Point = (1.0, 1.0)
    map_mode: int = 1                                    # MM_TEXT
    pen: Optional[Tuple[Tuple[int, int, int], float]] = ((0, 0, 0), 1.0)
    brush: Optional[Tuple[int, int, int]] = _WHITE
    text_colour: Tuple[int, int, int] = (0, 0, 0)
    position: Point = (0.0, 0.0)
    font: Optional[Tuple[int, int, int]] = None          # (height in logical units, escapement, weight)


@dataclass
class _Canvas:
    image: Image.Image
    origin: Point                                        # device point drawn at pixel (0, 0)
    state: _State = field(default_factory=_State)
    saved: List[_State] = field(default_factory=list)
    objects: Dict[int, tuple] = field(default_factory=dict)
    path: Optional[List[List[Point]]] = None

    @property
    def draw(self) -> ImageDraw.ImageDraw:
        return ImageDraw.Draw(self.image)

    def device(self, x: float, y: float) -> Point:
        """A logical point in image pixels: world transform, then the page (window → viewport)."""
        s, m = self.state, self.state.world
        wx, wy = x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5]
        sx = s.viewport_ext[0] / s.window_ext[0] if s.map_mode != 1 and s.window_ext[0] else 1.0
        sy = s.viewport_ext[1] / s.window_ext[1] if s.map_mode != 1 and s.window_ext[1] else 1.0
        dx = (wx - s.window_org[0]) * sx + s.viewport_org[0]
        dy = (wy - s.window_org[1]) * sy + s.viewport_org[1]
        return dx - self.origin[0], dy - self.origin[1]

    def scale(self) -> float:
        """How long one logical unit is in pixels (for pen widths)."""
        a = self.device(0, 0)
        b = self.device(1, 0)
        return max(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5, 1e-6)

    # --- drawing -------------------------------------------------------------------------------

    def stroke(self, points: List[Point]) -> None:
        if self.state.pen is None or len(points) < 2:
            return
        colour, width = self.state.pen
        self.draw.line(points, fill=colour, width=max(1, round(width * self.scale())), joint="curve")

    def fill(self, points: List[Point]) -> None:
        if self.state.brush is None or len(points) < 3:
            return
        self.draw.polygon(points, fill=self.state.brush)

    def shape(self, points: List[Point], closed: bool) -> None:
        """A figure: into the open path, or drawn now (filled when closed, then outlined)."""
        if self.path is not None:
            self.path.append(points)
            return
        if closed:
            self.fill(points)
            self.stroke(points + points[:1])
        else:
            self.stroke(points)


def _bezier(start: Point, rest: List[Point]) -> List[Point]:
    out, p0 = [start], start
    for i in range(0, len(rest) - 2, 3):
        p1, p2, p3 = rest[i], rest[i + 1], rest[i + 2]
        for step in range(1, _BEZIER_STEPS + 1):
            t = step / _BEZIER_STEPS
            u = 1 - t
            out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                        u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
        p0 = p3
    return out


def _points(data: bytes, offset: int, count: int, short: bool) -> List[Tuple[int, int]]:
    fmt, size = ("<hh", 4) if short else ("<ii", 8)
    return [struct.unpack_from(fmt, data, offset + i * size) for i in range(count)]


def _dib(data: bytes, offset: int, off_bmi: int, cb_bmi: int, off_bits: int, cb_bits: int) -> Optional[Image.Image]:
    """A device-independent bitmap stored in a record, as an RGB image."""
    if not cb_bmi or not cb_bits:
        return None
    bmi = data[offset + off_bmi: offset + off_bmi + cb_bmi]
    bits = data[offset + off_bits: offset + off_bits + cb_bits]
    header = b"BM" + struct.pack("<IHHI", 14 + len(bmi) + len(bits), 0, 0, 14 + len(bmi))
    try:
        return Image.open(io.BytesIO(header + bmi + bits)).convert("RGB")
    except Exception:
        return None


def _blit(canvas: _Canvas, box: Tuple[int, int, int, int], rop: int, bitmap: Optional[Image.Image]) -> None:
    """A bitmap (or the brush, for pattern operations) onto `box` with a raster operation."""
    left, top, right, bottom = box
    if right <= left or bottom <= top or rop == _NOP:
        return
    size = (right - left, bottom - top)
    if bitmap is None:
        if rop == _PATCOPY and canvas.state.brush is not None:
            source = Image.new("RGB", size, canvas.state.brush)
        elif rop == _WHITENESS:
            source = Image.new("RGB", size, _WHITE)
        elif rop == _BLACKNESS:
            source = Image.new("RGB", size, (0, 0, 0))
        elif rop == _PATINVERT and canvas.state.brush is not None:
            # XOR with the brush colour, band by band (Excel inverts and restores a legend swatch).
            region = canvas.image.crop(box)
            bands = [band.point(lambda v, k=k: v ^ k) for band, k in zip(region.split(), canvas.state.brush)]
            canvas.image.paste(Image.merge("RGB", bands), (left, top))
            return
        else:
            return
        canvas.image.paste(source, (left, top))
        return
    source = bitmap.resize(size) if bitmap.size != size else bitmap
    region = canvas.image.crop(box)
    if rop == _SRCPAINT:          # OR: with a 0/255 mask, the lighter of the two
        source = ImageChops.lighter(region, source)
    elif rop == _SRCAND:          # AND: with white around the picture, the darker of the two
        source = ImageChops.darker(region, source)
    canvas.image.paste(source, (left, top))


def _font(height: float) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=max(8, int(height * 0.8)))
    except TypeError:                                  # Pillow < 10.1 has one fixed-size font
        return ImageFont.load_default()


def _text(c: _Canvas, text: str, box: Tuple[float, float, float, float]) -> None:
    """A text in its device box, in the selected font's size, weight and direction. A text turned by
    90° or 270°, by its font's escapement or by the world transform (Excel's sideways label inside a
    bar), is drawn on its own strip and turned upright."""
    left, top, right, bottom = box
    s = c.state
    height, escapement, weight = s.font or (0, 0, 400)
    size = height * c.scale() if height else (bottom - top)
    font = _font(size / 0.8 if height else size)
    bold = 1 if weight >= 600 else 0
    m = s.world
    turned = math.degrees(math.atan2(-m[1], m[0])) if (m[0] or m[1]) else 0.0   # (0,-1,1,0) → 90°
    angle = round((escapement or 0) / 10 + turned) % 360
    if angle not in (90, 270):
        c.draw.text((left, top), text, fill=s.text_colour, font=font, stroke_width=bold, stroke_fill=s.text_colour)
        return
    x0, y0, x1, y1 = c.draw.textbbox((0, 0), text, font=font, stroke_width=bold)
    strip = Image.new("RGBA", (max(1, int(x1) + 2), max(1, int(y1) + 2)), (255, 255, 255, 0))
    ImageDraw.Draw(strip).text((0, 0), text, fill=s.text_colour + (255,), font=font,
                               stroke_width=bold, stroke_fill=s.text_colour + (255,))
    upright = strip.rotate(angle, expand=True)
    box_w, box_h = max(1, int(right - left)), max(1, int(bottom - top))
    if upright.width > box_w or upright.height > box_h:
        upright.thumbnail((box_w, box_h))
    c.image.paste(upright, (int(left + (box_w - upright.width) / 2), int(top + (box_h - upright.height) / 2)), upright)


def render_emf(data: bytes, max_px: int = 1600) -> Optional[bytes]:
    """The EMF drawn as a PNG whose long side is at most `max_px`; None when `data` is not an EMF or
    draws nothing."""
    if (len(data) < 88 or struct.unpack_from("<I", data, 0)[0] != _EMR_HEADER
            or data[40:44] != _EMF_SIGNATURE):
        return None
    left, top, right, bottom = struct.unpack_from("<iiii", data, 8)
    width, height = right - left + 1, bottom - top + 1
    if width <= 1 or height <= 1 or width * height > 60_000_000:
        return None
    canvas = _Canvas(Image.new("RGB", (width, height), _WHITE), origin=(left, top))
    drew = False
    offset = 0
    while offset + 8 <= len(data):
        record_type, size = struct.unpack_from("<II", data, offset)
        if size < 8 or offset + size > len(data):
            break
        try:
            drew |= _record(canvas, data, offset, record_type, size)
        except Exception:
            logger.debug("EMF record %d at %d not drawn", record_type, offset, exc_info=True)
        if record_type == _EMR_EOF:
            break
        offset += size
    if not drew:
        return None
    image = canvas.image
    if max(image.size) > max_px:
        ratio = max_px / max(image.size)
        image = image.resize((max(1, round(image.width * ratio)), max(1, round(image.height * ratio))), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _record(c: _Canvas, data: bytes, off: int, t: int, size: int) -> bool:
    """Apply one record; True when it drew something."""
    s = c.state
    i32 = lambda at: struct.unpack_from("<i", data, off + at)[0]   # noqa: E731
    u32 = lambda at: struct.unpack_from("<I", data, off + at)[0]   # noqa: E731
    if t == 9:
        s.window_ext = (i32(8), i32(12))
    elif t == 10:
        s.window_org = (i32(8), i32(12))
    elif t == 11:
        s.viewport_ext = (i32(8), i32(12))
    elif t == 12:
        s.viewport_org = (i32(8), i32(12))
    elif t == 17:
        s.map_mode = u32(8)
    elif t == 24:
        s.text_colour = _colour(u32(8))
    elif t == 33:
        c.saved.append(replace(s))
    elif t == 34:
        back = abs(i32(8)) or 1
        for _ in range(back):
            if c.saved:
                c.state = c.saved.pop()
    elif t == 35:
        s.world = struct.unpack_from("<6f", data, off + 8)
    elif t == 36:
        xform, mode = struct.unpack_from("<6f", data, off + 8), u32(32)
        s.world = (_IDENTITY if mode == 1 else _multiply(xform, s.world) if mode == 2
                   else _multiply(s.world, xform) if mode == 3 else xform)
    elif t == 37:
        index = u32(8)
        if index & 0x80000000:
            stock = index & 0x7FFFFFFF
            if stock in _STOCK_BRUSHES:
                s.brush = _STOCK_BRUSHES[stock]
            elif stock in _STOCK_PENS:
                s.pen = _STOCK_PENS[stock]
        elif index in c.objects:
            kind, value = c.objects[index]
            if kind == "pen":
                s.pen = value
            elif kind == "font":
                s.font = value
            else:
                s.brush = value
    elif t == 38:                                   # CREATEPEN
        style, width, colour = u32(12), i32(16), u32(24)
        c.objects[u32(8)] = ("pen", None if style & 0xF == 5 else (_colour(colour), max(width, 1)))
    elif t == 95:                                   # EXTCREATEPEN
        style, width, colour = u32(28), u32(32), u32(40)
        c.objects[u32(8)] = ("pen", None if style & 0xF == 5 else (_colour(colour), max(width, 1)))
    elif t == 39:                                   # CREATEBRUSHINDIRECT
        style, colour = u32(12), u32(16)
        c.objects[u32(8)] = ("brush", None if style == 1 else _colour(colour))
    elif t == 82:                                   # EXTCREATEFONTINDIRECTW: height, escapement, weight
        c.objects[u32(8)] = ("font", (abs(i32(12)), i32(20), i32(28)))
    elif t == 40:
        c.objects.pop(u32(8), None)
    elif t == 27:                                   # MOVETOEX
        s.position = (i32(8), i32(12))
        if c.path is not None:
            c.path.append([c.device(*s.position)])
    elif t == 54:                                   # LINETO
        start, end = c.device(*s.position), c.device(i32(8), i32(12))
        s.position = (i32(8), i32(12))
        if c.path is not None:
            (c.path[-1] if c.path else c.path.append([start]) or c.path[-1]).append(end)
        else:
            c.stroke([start, end])
        return True
    elif t in (2, 3, 4, 85, 86, 87):                # POLYBEZIER, POLYGON, POLYLINE (32 and 16 bit)
        short = t >= 85
        pts = [c.device(x, y) for x, y in _points(data, off + 28, u32(24), short)]
        if t in (2, 85):
            pts = _bezier(pts[0], pts[1:]) if pts else pts
        c.shape(pts, closed=t in (3, 86))
        return True
    elif t in (5, 6, 88, 89):                       # POLYBEZIERTO, POLYLINETO
        short = t >= 88
        raw = _points(data, off + 28, u32(24), short)
        start = c.device(*s.position)
        pts = [c.device(x, y) for x, y in raw]
        pts = _bezier(start, pts) if t in (5, 88) else [start] + pts
        if raw:
            s.position = raw[-1]
        if c.path is not None:
            if c.path and c.path[-1] and c.path[-1][-1] == start:
                c.path[-1].extend(pts[1:])
            else:
                c.path.append(pts)
        else:
            c.stroke(pts)
        return True
    elif t in (90, 91):                             # POLYPOLYLINE16, POLYPOLYGON16
        n_polys, total = u32(24), u32(28)
        counts = [u32(32 + 4 * k) for k in range(n_polys)]
        pts = [c.device(x, y) for x, y in _points(data, off + 32 + 4 * n_polys, total, True)]
        at = 0
        for count in counts:
            c.shape(pts[at:at + count], closed=t == 91)
            at += count
        return True
    elif t in (42, 43):                             # ELLIPSE, RECTANGLE
        l, tp, r, b = (i32(8), i32(12), i32(16), i32(20))
        (x0, y0), (x1, y1) = c.device(l, tp), c.device(r, b)
        box = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
        pen = s.pen
        outline, width = (pen[0], max(1, round(pen[1] * c.scale()))) if pen else (None, 0)
        draw = c.draw.ellipse if t == 42 else c.draw.rectangle
        draw(box, fill=s.brush, outline=outline, width=width)
        return True
    elif t == 59:                                   # BEGINPATH
        c.path = []
    elif t == 60:                                   # ENDPATH
        pass
    elif t == 61:                                   # CLOSEFIGURE
        if c.path and c.path[-1]:
            c.path[-1].append(c.path[-1][0])
    elif t in (62, 63, 64):                         # FILLPATH, STROKEANDFILLPATH, STROKEPATH
        figures, c.path = c.path or [], None
        for figure in figures:
            if t in (62, 63):
                c.fill(figure)
            if t in (63, 64):
                c.stroke(figure)
        return bool(figures)
    elif t == 84:                                   # EXTTEXTOUTW: drawn at its device bounds
        l, tp, r, b = (i32(8), i32(12), i32(16), i32(20))
        n_chars, off_string = u32(44), u32(48)
        text = data[off + off_string: off + off_string + 2 * n_chars].decode("utf-16-le", "replace").strip()
        if not text or (l, tp, r, b) == (0, 0, 0, 0):
            return False
        _text(c, text, (l - c.origin[0], tp - c.origin[1], r - c.origin[0], b - c.origin[1]))
        return True
    elif t == 81:                                   # STRETCHDIBITS
        x, y = i32(24), i32(28)
        off_bmi, cb_bmi, off_bits, cb_bits = u32(48), u32(52), u32(56), u32(60)
        rop, cx, cy = u32(68), i32(72), i32(76)
        (x0, y0), (x1, y1) = c.device(x, y), c.device(x + cx, y + cy)
        box = (round(min(x0, x1)), round(min(y0, y1)), round(max(x0, x1)), round(max(y0, y1)))
        bitmap = _dib(data, off, off_bmi, cb_bmi, off_bits, cb_bits)
        if bitmap is not None and (y1 < y0):
            bitmap = bitmap.transpose(Image.FLIP_TOP_BOTTOM)
        _blit(c, box, rop, bitmap)
        return True
    elif t == 76:                                   # BITBLT
        x, y, cx, cy, rop = i32(24), i32(28), i32(32), i32(36), u32(40)
        off_bmi, cb_bmi, off_bits, cb_bits = u32(84), u32(88), u32(92), u32(96)
        (x0, y0), (x1, y1) = c.device(x, y), c.device(x + cx, y + cy)
        box = (round(min(x0, x1)), round(min(y0, y1)), round(max(x0, x1)), round(max(y0, y1)))
        _blit(c, box, rop, _dib(data, off, off_bmi, cb_bmi, off_bits, cb_bits))
        return rop != _NOP
    return False
