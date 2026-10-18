"""Vector PDF to CAD 2D IR importer backed by :mod:`pypdfium2` (PDFium).

A PDF plotted from CAD keeps the geometry (paths, Bezier curves), the pen
attributes (width, colour, dash) and usually the text as text objects, but it
has forgotten the CAD structure: an arc is four Bezier curves, a layer is at
best an optional-content group, a dimension is lines plus a number, and the
coordinates are paper points. This importer reads the page objects natively
through PDFium and rebuilds what can be rebuilt deterministically:

- stroked paths become ``LINE`` / ``LWPOLYLINE`` entities; cubic Beziers that
  are circular-arc approximations (control distance ``4/3 tan(t/4) r``, the
  construction every CAD plotter uses) become ``ARC`` / ``CIRCLE``; other
  curves stay exact as cubic ``SPLINE`` entities; polylines that trace a circle
  with many short, evenly turning segments are refitted to arcs
  (``PDF_POLYLINE_ARC_REFITTED``);
- pen width becomes ``lineweight_mm``, dash arrays become ``tables.linetypes``
  entries named by their pattern class (``DASHED``, ``DASHDOT``, ``CENTER`` ...),
  optional-content group names become layers, colours are kept;
- filled paths become solid ``HATCH`` entities; thin filled rectangles that
  stand in for thick lines become ``LINE`` entities with the rectangle width;
- text comes from PDFium's text page: glyph runs are regrouped into ``TEXT``
  entities with insert, height, rotation, width factor and font, invisible OCR
  text (render mode 3) is skipped;
- raster images keep their placement as ``IMAGE`` entities (pixels embedded on
  request);
- coordinates are paper millimetres from the lower-left corner of the page as
  displayed (``/Rotate`` and ``/CropBox`` applied); with ``scale="auto"`` the
  plot scale is recovered from the dimension numbers and their extension lines
  (:mod:`cad2d_ir.paper_scale`) and, when enough dimensions agree, the drawing
  is scaled to model millimetres (``PDF_SCALE_ESTIMATED``). Multiple pages are
  laid out side by side along +X like IDW sheets (``PDF_PAGES_TILED``).

Rectangular clips are applied exactly to lines, polylines, arcs and circles;
other clip shapes keep the clipped objects whole and are disclosed. Everything
that is inferred rather than read is disclosed through diagnostics and counted
in ``statistics``.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import math
import struct
import zlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cad2d_ir import paper_scale
from cad2d_ir.importers.base import (
    ImportDiagnostic,
    ImporterError,
    ImportOptions,
    ImportResult,
    MissingOptionalDependencyError,
)
from cad2d_ir.schema import validate_ir

PT_TO_MM = 25.4 / 72.0
DEFAULT_PAGE_GAP_RATIO = 0.1
# Geometric tolerance of the arc fits and merges, in paper millimetres. 0.02 mm is
# a fifth of the thinnest plotted line and well below PDFium's float precision
# loss on an A0 sheet.
FIT_TOLERANCE_MM = 0.02
# Flattening tolerance of curved hatch boundaries (HATCH loops are polygons).
FLATTEN_TOLERANCE_MM = 0.05
# A filled rectangle at most this wide (and at least 4x longer) is a thick line.
FILL_AS_LINE_MAX_WIDTH_MM = 1.0
# A closed stroked shape whose extent is at most this is a point marker.
POINT_SHAPE_MAX_MM = 0.1
MAX_FORM_DEPTH = 16
# Pixels above which an image is not embedded even with embed_images=True.
MAX_EMBED_PIXELS = 16_000_000

_SEG_LINETO, _SEG_BEZIERTO, _SEG_MOVETO = 0, 1, 2
_OBJ_TEXT, _OBJ_PATH, _OBJ_IMAGE, _OBJ_SHADING, _OBJ_FORM = 1, 2, 3, 4, 5
_PDF_OBJECT_STRING = 3
_EPS = 1e-9
_TWO_PI = 2.0 * math.pi
_CJK_RANGES = (
    (0x3000, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)

Matrix = tuple[float, float, float, float, float, float]
Point = tuple[float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


# --------------------------------------------------------------------------- math


def _concat(first: Matrix, second: Matrix) -> Matrix:
    """Matrix that applies ``first`` and then ``second`` (PDF row-vector order)."""
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = second
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _apply(m: Matrix, x: float, y: float) -> Point:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _apply_vector(m: Matrix, x: float, y: float) -> Point:
    return (m[0] * x + m[2] * y, m[1] * x + m[3] * y)


def _linear_scale(m: Matrix) -> float:
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _angle_deg(x: float, y: float) -> float:
    return _norm_deg(math.degrees(math.atan2(y, x)))


def _norm_deg(value: float) -> float:
    """Angle in [0, 360) with float noise just below 360 snapped to 0."""
    value %= 360.0
    return 0.0 if value >= 360.0 - 1e-4 or value <= 1e-4 else value


def _bezier_point(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    u = 1.0 - t
    b0, b1, b2, b3 = u * u * u, 3.0 * u * u * t, 3.0 * u * t * t, t * t * t
    return (
        b0 * p0[0] + b1 * p1[0] + b2 * p2[0] + b3 * p3[0],
        b0 * p0[1] + b1 * p1[1] + b2 * p2[1] + b3 * p3[1],
    )


def _flatten_bezier(
    p0: Point,
    p1: Point,
    p2: Point,
    p3: Point,
    tol: float,
    out: list[Point],
    depth: int = 0,
) -> None:
    """Append points after ``p0`` that follow the curve within ``tol``."""
    # Flatness: distance of the control points from the chord.
    dx, dy = p3[0] - p0[0], p3[1] - p0[1]
    chord = math.hypot(dx, dy)
    if chord < _EPS:
        d1 = _dist(p1, p0)
        d2 = _dist(p2, p0)
    else:
        d1 = abs((p1[0] - p0[0]) * dy - (p1[1] - p0[1]) * dx) / chord
        d2 = abs((p2[0] - p0[0]) * dy - (p2[1] - p0[1]) * dx) / chord
    if (d1 <= tol and d2 <= tol) or depth >= 12:
        out.append(p3)
        return
    # de Casteljau split at t = 0.5
    p01 = ((p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0)
    p12 = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
    p23 = ((p2[0] + p3[0]) / 2.0, (p2[1] + p3[1]) / 2.0)
    p012 = ((p01[0] + p12[0]) / 2.0, (p01[1] + p12[1]) / 2.0)
    p123 = ((p12[0] + p23[0]) / 2.0, (p12[1] + p23[1]) / 2.0)
    mid = ((p012[0] + p123[0]) / 2.0, (p012[1] + p123[1]) / 2.0)
    _flatten_bezier(p0, p01, p012, mid, tol, out, depth + 1)
    _flatten_bezier(mid, p123, p23, p3, tol, out, depth + 1)


@dataclass(slots=True)
class _Arc:
    """Circular arc from ``start`` sweeping ``sweep`` radians (positive = CCW)."""

    center: Point
    radius: float
    start: float  # radians
    sweep: float  # signed radians
    p0: Point
    p1: Point

    @property
    def ccw(self) -> bool:
        return self.sweep > 0.0

    @property
    def end(self) -> float:
        return self.start + self.sweep

    def point_at(self, angle: float) -> Point:
        return (
            self.center[0] + self.radius * math.cos(angle),
            self.center[1] + self.radius * math.sin(angle),
        )


def _sweep_between(start: float, end: float, ccw: bool) -> float:
    """Signed sweep from ``start`` to ``end`` going the given way, in (0, 2pi]."""
    if ccw:
        sweep = (end - start) % _TWO_PI
        return sweep if sweep > _EPS else _TWO_PI
    sweep = (start - end) % _TWO_PI
    return -(sweep if sweep > _EPS else _TWO_PI)


def _bezier_arc(p0: Point, p1: Point, p2: Point, p3: Point, tol: float) -> _Arc | None:
    """The circular arc this cubic Bezier approximates, or ``None``.

    A CAD plotter writes an arc of angle ``t`` as a cubic whose control points lie
    on the end tangents at distance ``4/3 tan(t/4) r``. We intersect the normals at
    both ends for the centre, then require the end radii, the curve samples and
    the control distances to agree with that circle.
    """
    t0 = (p1[0] - p0[0], p1[1] - p0[1])
    t1 = (p3[0] - p2[0], p3[1] - p2[1])
    chord = (p3[0] - p0[0], p3[1] - p0[1])
    chord_len = math.hypot(*chord)
    len0, len1 = math.hypot(*t0), math.hypot(*t1)
    if chord_len < 1e-7 or len0 < _EPS or len1 < _EPS:
        return None
    # Tiny curves (point markers plotted as circles) must pass tests relative to
    # their own size, or an absolute tolerance would accept any shape.
    tol = min(tol, 0.1 * chord_len)
    if abs(len0 - len1) > 0.1 * max(len0, len1):
        return None
    # Tangents must be symmetric about the chord (same angle, mirrored sign).
    s0 = _cross(chord, t0) / (chord_len * len0)
    s1 = _cross(t1, chord) / (chord_len * len1)
    if abs(s0 - s1) > 0.02 or abs(s0) < 1e-4:
        return None
    n0 = (-t0[1] / len0, t0[0] / len0)
    n1 = (-t1[1] / len1, t1[0] / len1)
    det = _cross(n0, n1)
    if abs(det) < 1e-6:
        # Parallel normals: a semicircle. The centre is the chord midpoint.
        center = ((p0[0] + p3[0]) / 2.0, (p0[1] + p3[1]) / 2.0)
    else:
        # p0 + s*n0 = p3 + u*n1  ->  s = cross(p3 - p0, n1) / cross(n0, n1)
        s = _cross(chord, n1) / det
        center = (p0[0] + s * n0[0], p0[1] + s * n0[1])
    radius = _dist(center, p0)
    if radius < 1e-7 or abs(_dist(center, p3) - radius) > tol:
        return None
    ccw = _cross((p0[0] - center[0], p0[1] - center[1]), t0) > 0.0
    start = math.atan2(p0[1] - center[1], p0[0] - center[0])
    end = math.atan2(p3[1] - center[1], p3[0] - center[0])
    sweep = _sweep_between(start, end, ccw)
    if abs(sweep) > math.pi + 1e-3:
        return None
    expected = 4.0 / 3.0 * math.tan(abs(sweep) / 4.0) * radius
    if abs(len0 - expected) > 0.1 * expected + tol:
        return None
    # A 90-degree Bezier piece deviates from its circle by up to 2.7e-4 r by
    # construction; smaller sweeps deviate far less.
    rtol = max(tol, 4e-4 * radius)
    for t in (0.25, 0.5, 0.75):
        q = _bezier_point(p0, p1, p2, p3, t)
        if abs(_dist(center, q) - radius) > rtol:
            return None
    return _Arc(center, radius, start, sweep, p0, p3)


def _fit_circle(points: Sequence[Point]) -> tuple[Point, float] | None:
    """Algebraic (Kasa) least-squares circle through ``points``."""
    n = len(points)
    if n < 3:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    suu = suv = svv = suuu = svvv = suvv = svuu = 0.0
    for x, y in points:
        u, v = x - mx, y - my
        suu += u * u
        svv += v * v
        suv += u * v
        suuu += u * u * u
        svvv += v * v * v
        suvv += u * v * v
        svuu += v * u * u
    det = suu * svv - suv * suv
    if abs(det) < 1e-12:
        return None
    rhs_u = (suuu + suvv) / 2.0
    rhs_v = (svvv + svuu) / 2.0
    uc = (rhs_u * svv - rhs_v * suv) / det
    vc = (rhs_v * suu - rhs_u * suv) / det
    radius = math.sqrt(uc * uc + vc * vc + (suu + svv) / n)
    return (uc + mx, vc + my), radius


def _refit_polyline(points: list[Point], closed: bool, tol: float) -> list[Any]:
    """Split a polyline into arcs (where it traces a circle) and straight runs.

    Returns a list of ``("L", a, b)`` and ``("A", arc)`` items in path order. An
    arc needs at least six consecutive segments of similar length that all turn
    the same way by 0.5 to 60 degrees and lie within ``tol`` of one circle.
    """
    pts = list(points)
    if closed and len(pts) > 2 and _dist(pts[0], pts[-1]) > _EPS:
        pts.append(pts[0])
    n_seg = len(pts) - 1
    items: list[Any] = []
    if n_seg < 6:
        return [("L", pts[i], pts[i + 1]) for i in range(n_seg)]
    lengths = [_dist(pts[i], pts[i + 1]) for i in range(n_seg)]
    turns: list[float | None] = [None] * n_seg  # turn entering segment i
    for i in range(1, n_seg):
        if lengths[i - 1] < _EPS or lengths[i] < _EPS:
            continue
        d0 = (
            (pts[i][0] - pts[i - 1][0]) / lengths[i - 1],
            (pts[i][1] - pts[i - 1][1]) / lengths[i - 1],
        )
        d1 = (
            (pts[i + 1][0] - pts[i][0]) / lengths[i],
            (pts[i + 1][1] - pts[i][1]) / lengths[i],
        )
        turns[i] = math.atan2(_cross(d0, d1), d0[0] * d1[0] + d0[1] * d1[1])
    i = 0
    pending_line_from = 0
    while i < n_seg:
        # Grow a window [i, j) of segments turning consistently.
        j = i + 1
        while j < n_seg:
            turn = turns[j]
            prev = turns[j - 1] if j - 1 > i else turns[j]
            if (
                turn is None
                or abs(turn) < math.radians(0.5)
                or abs(turn) > math.radians(60.0)
            ):
                break
            if prev is not None and j - 1 > i and (turn * prev <= 0.0):
                break
            if (
                prev is not None
                and j - 1 > i
                and abs(abs(turn) - abs(prev)) > 0.5 * max(abs(turn), abs(prev))
            ):
                break
            ratio = lengths[j] / lengths[i] if lengths[i] > _EPS else 0.0
            if not (0.4 <= ratio <= 2.5):
                break
            j += 1
        window = pts[i : j + 1]
        arc = _window_arc(window, tol) if j - i >= 6 else None
        if arc is None:
            i += 1
            continue
        for k in range(pending_line_from, i):
            items.append(("L", pts[k], pts[k + 1]))
        items.append(("A", arc))
        i = j
        pending_line_from = j
    for k in range(pending_line_from, n_seg):
        items.append(("L", pts[k], pts[k + 1]))
    return items


def _window_arc(window: Sequence[Point], tol: float) -> _Arc | None:
    fit = _fit_circle(window)
    if fit is None:
        return None
    center, radius = fit
    if radius < tol:
        return None
    limit = max(tol, 2e-3 * radius)
    if any(abs(_dist(center, p) - radius) > limit for p in window):
        return None
    p0, p1 = window[0], window[-1]
    d0 = (window[1][0] - p0[0], window[1][1] - p0[1])
    ccw = _cross((p0[0] - center[0], p0[1] - center[1]), d0) > 0.0
    start = math.atan2(p0[1] - center[1], p0[0] - center[0])
    end = math.atan2(p1[1] - center[1], p1[0] - center[0])
    if _dist(p0, p1) <= limit:
        sweep = _TWO_PI if ccw else -_TWO_PI
    else:
        sweep = _sweep_between(start, end, ccw)
    if abs(sweep) < math.radians(10.0):
        return None
    # The polyline must actually go around: the summed turning agrees with the sweep.
    return _Arc(center, radius, start, sweep, p0, p1)


def _merge_arcs(a: _Arc, b: _Arc, tol: float) -> _Arc | None:
    limit = max(tol, 1e-4 * a.radius)
    if a.ccw != b.ccw or abs(a.radius - b.radius) > limit:
        return None
    if _dist(a.center, b.center) > limit or _dist(a.p1, b.p0) > limit:
        return None
    sweep = a.sweep + b.sweep
    if abs(sweep) > _TWO_PI + 1e-6:
        return None
    return _Arc(a.center, a.radius, a.start, sweep, a.p0, b.p1)


# ----------------------------------------------------------------- PDFium helpers


def _load_pdfium() -> tuple[Any, Any]:
    try:
        import pypdfium2
        import pypdfium2.raw as raw
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "PDF support requires the optional dependency pypdfium2; "
            'install it with `pip install "cad2d-ir[pdf]"`.'
        ) from exc
    return pypdfium2, raw


def _utf16_buffer(fn: Any, *args: Any, size: int = 256) -> str:
    """Call a PDFium ``(…, buffer, buflen)`` UTF-16 getter and decode the result."""
    buf = (ctypes.c_ushort * size)()
    length = fn(*args, buf, size * 2)
    if not length:
        return ""
    count = length // 2 - 1 if length >= 2 else 0
    if count > size:
        buf = (ctypes.c_ushort * (count + 1))()
        fn(*args, buf, (count + 1) * 2)
    return "".join(chr(c) for c in buf[:count] if c)


def _utf16_outlen(fn: Any, *args: Any, size: int = 256) -> str:
    """Call a getter with ``(…, buffer, buflen, out_len*)`` and decode the result."""
    buf = (ctypes.c_ushort * size)()
    out_len = ctypes.c_ulong()
    if not fn(*args, buf, size * 2, ctypes.byref(out_len)):
        return ""
    count = max(0, int(out_len.value) // 2 - 1)
    return "".join(chr(c) for c in buf[: min(count, size)] if c)


def _matrix_of(raw: Any, obj: Any) -> Matrix:
    m = raw.FS_MATRIX()
    if not raw.FPDFPageObj_GetMatrix(obj, ctypes.byref(m)):
        return _IDENTITY
    return (float(m.a), float(m.b), float(m.c), float(m.d), float(m.e), float(m.f))


def _rgba(raw: Any, fn: Any, obj: Any) -> tuple[int, int, int, int] | None:
    r, g, b, a = (ctypes.c_uint() for _ in range(4))
    if not fn(obj, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a)):
        return None
    return (int(r.value), int(g.value), int(b.value), int(a.value))


def _color_hex(rgba: tuple[int, int, int, int] | None) -> str | None:
    if rgba is None:
        return None
    r, g, b, a = (max(0, min(255, v)) for v in rgba)
    if a < 255:
        return f"#{r:02X}{g:02X}{b:02X}{a:02X}"
    return f"#{r:02X}{g:02X}{b:02X}"


def _segments(raw: Any, obj: Any) -> list[tuple[int, float, float, bool]]:
    count = raw.FPDFPath_CountSegments(obj)
    result = []
    x, y = ctypes.c_float(), ctypes.c_float()
    for i in range(count):
        seg = raw.FPDFPath_GetPathSegment(obj, i)
        if not seg:
            continue
        raw.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y))
        result.append(
            (
                int(raw.FPDFPathSegment_GetType(seg)),
                float(x.value),
                float(y.value),
                bool(raw.FPDFPathSegment_GetClose(seg)),
            )
        )
    return result


def _clip_segments(raw: Any, obj: Any) -> list[list[tuple[int, float, float]]] | None:
    clip = raw.FPDFPageObj_GetClipPath(obj)
    if not clip:
        return None
    paths = []
    x, y = ctypes.c_float(), ctypes.c_float()
    for i in range(raw.FPDFClipPath_CountPaths(clip)):
        segs = []
        for j in range(raw.FPDFClipPath_CountPathSegments(clip, i)):
            seg = raw.FPDFClipPath_GetPathSegment(clip, i, j)
            if not seg:
                continue
            raw.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y))
            segs.append(
                (int(raw.FPDFPathSegment_GetType(seg)), float(x.value), float(y.value))
            )
        if segs:
            paths.append(segs)
    return paths or None


def _ocg_name(raw: Any, obj: Any) -> str | None:
    """Optional-content group name from an ``/OC`` marked-content mark, if any."""
    try:
        count = raw.FPDFPageObj_CountMarks(obj)
    except Exception:  # noqa: BLE001 - old PDFium without marks API
        return None
    for i in range(count):
        mark = raw.FPDFPageObj_GetMark(obj, i)
        if not mark:
            continue
        name = _utf16_outlen(raw.FPDFPageObjMark_GetName, mark, size=64)
        if name != "OC":
            continue
        for j in range(raw.FPDFPageObjMark_CountParams(mark)):
            key = _utf16_outlen(raw.FPDFPageObjMark_GetParamKey, mark, j, size=64)
            if key != "Name":
                continue
            if (
                raw.FPDFPageObjMark_GetParamValueType(mark, key.encode())
                != _PDF_OBJECT_STRING
            ):
                continue
            value = _decode_pdf_text(_mark_blob(raw, mark, key))
            if value:
                return value
    return None


def _mark_blob(raw: Any, mark: Any, key: str) -> bytes:
    """Raw bytes of a string mark parameter.

    PDFium's string getter re-decodes the bytes as UTF-8, which garbles the
    UTF-16 or CP932 names Japanese producers write for optional content groups.
    """
    getter = getattr(raw, "FPDFPageObjMark_GetParamBlobValue", None)
    if getter is None:
        return _utf16_outlen(
            raw.FPDFPageObjMark_GetParamStringValue, mark, key.encode(), size=128
        ).encode("utf-8")
    out_len = ctypes.c_ulong()
    if (
        not getter(mark, key.encode(), None, 0, ctypes.byref(out_len))
        or not out_len.value
    ):
        return b""
    buf = (ctypes.c_ubyte * int(out_len.value))()
    if not getter(mark, key.encode(), buf, int(out_len.value), ctypes.byref(out_len)):
        return b""
    return bytes(buf[: int(out_len.value)])


def _decode_pdf_text(data: bytes) -> str:
    """Decode a PDF text string: UTF-16 with BOM, UTF-8, CP932, else Latin-1."""
    if not data:
        return ""
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be", "replace").rstrip("\x00")
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le", "replace").rstrip("\x00")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _png_bytes(width: int, height: int, mode: str, buffer: Any, stride: int) -> bytes:
    """Encode a PDFium bitmap buffer (``L``/``BGR``/``BGRA``/``BGRx``) as PNG."""
    channels = {"L": 1, "BGR": 3, "RGB": 3, "BGRA": 4, "RGBA": 4, "BGRx": 4, "RGBx": 4}[
        mode
    ]
    color_type = {1: 0, 3: 2, 4: 6}[channels]
    if mode in ("BGRx", "RGBx"):
        color_type = 2
    data = bytes(buffer)
    rows = bytearray()
    for y in range(height):
        row = data[y * stride : y * stride + width * channels]
        if mode in ("BGR", "BGRA", "BGRx"):
            px = bytearray(row)
            px[0::channels], px[2::channels] = row[2::channels], row[0::channels]
            row = bytes(px)
        if mode in ("BGRx", "RGBx"):
            row = b"".join(row[i : i + 3] for i in range(0, len(row), 4))
        rows += b"\x00" + row

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + tag
            + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )


# ------------------------------------------------------------------- conversion


@dataclass(slots=True)
class _PageFrame:
    """Where one PDF page sits in the IR and how its user space maps there."""

    index: int  # 0-based
    width_mm: float
    height_mm: float
    rotation: int
    offset_x: float
    page_map: Matrix  # user space (pt) -> IR paper mm
    crop: tuple[float, float, float, float]

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return (self.offset_x, 0.0, self.offset_x + self.width_mm, self.height_mm)


def _page_frame(page: Any, index: int, offset_x: float) -> _PageFrame:
    mx0, my0, mx1, my1 = page.get_mediabox()
    try:
        cx0, cy0, cx1, cy1 = page.get_cropbox()
    except Exception:  # noqa: BLE001
        cx0, cy0, cx1, cy1 = mx0, my0, mx1, my1
    x0, y0 = max(mx0, cx0), max(my0, cy0)
    x1, y1 = min(mx1, cx1), min(my1, cy1)
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        x0, y0, x1, y1 = mx0, my0, mx1, my1
    rotation = int(page.get_rotation()) % 360
    k = PT_TO_MM
    if rotation == 90:
        page_map = (0.0, -k, k, 0.0, -y0 * k + offset_x, x1 * k)
        width, height = (y1 - y0) * k, (x1 - x0) * k
    elif rotation == 180:
        page_map = (-k, 0.0, 0.0, -k, x1 * k + offset_x, y1 * k)
        width, height = (x1 - x0) * k, (y1 - y0) * k
    elif rotation == 270:
        page_map = (0.0, k, -k, 0.0, y1 * k + offset_x, -x0 * k)
        width, height = (y1 - y0) * k, (x1 - x0) * k
    else:
        page_map = (k, 0.0, 0.0, k, -x0 * k + offset_x, -y0 * k)
        width, height = (x1 - x0) * k, (y1 - y0) * k
    return _PageFrame(
        index, width, height, rotation, offset_x, page_map, (x0, y0, x1, y1)
    )


@dataclass(slots=True)
class _Context:
    options: ImportOptions
    embed_images: bool
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    linetypes: dict[str, dict[str, Any]] = field(default_factory=dict)
    text_styles: dict[str, dict[str, Any]] = field(default_factory=dict)
    linetype_by_pattern: dict[tuple[float, ...], str] = field(default_factory=dict)
    counts: Counter[str] = field(default_factory=Counter)
    source_counts: Counter[str] = field(default_factory=Counter)
    next_entity_number: int = 1

    def allocate_id(self) -> str:
        entity_id = f"PDF_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id

    def note(self, key: str, amount: int = 1) -> None:
        if amount:
            self.counts[key] += amount

    def layer(self, name: str | None) -> str:
        layer_name = name or "0"
        self.layers.setdefault(layer_name, {})
        return layer_name

    def linetype(self, dashes_mm: Sequence[float]) -> str:
        pattern = _normalize_dash(dashes_mm)
        if not pattern:
            return "CONTINUOUS"
        key = tuple(pattern)
        name = self.linetype_by_pattern.get(key)
        if name is not None:
            return name
        base = _classify_dash(pattern)
        name = base
        suffix = 2
        while name in self.linetypes:
            name = f"{base}_{suffix}"
            suffix += 1
        self.linetypes[name] = {
            "description": "PDF dash pattern "
            + " ".join(f"{abs(v):g}" for v in pattern),
            "pattern_mm": list(pattern),
        }
        self.linetype_by_pattern[key] = name
        return name

    def text_style(self, font: str) -> str:
        name = "PDF_" + "".join(
            ch if ch.isalnum() or ch in "_-" else "_" for ch in font
        )
        self.text_styles.setdefault(name, {"font": font})
        return name


def _normalize_dash(dashes_mm: Sequence[float]) -> list[float]:
    values = [round(float(v), 3) for v in dashes_mm if math.isfinite(float(v))]
    if not values or all(v <= 0.0 for v in values):
        return []
    if len(values) % 2:
        values = values * 2
    pattern: list[float] = []
    for i, v in enumerate(values):
        if i % 2 == 0:
            pattern.append(max(v, 0.0))
        else:
            pattern.append(-v if v > 0 else 0.0)
    return pattern


def _classify_dash(pattern: Sequence[float]) -> str:
    dashes = [v for v in pattern[0::2]]
    dot = 0.6
    if all(v <= dot for v in dashes):
        return "DOT"
    if len(pattern) == 2:
        return "DASHED"
    if len(pattern) == 4:
        long_, short = dashes[0], dashes[1]
        if short <= dot or short <= 0.25 * long_:
            return "DASHDOT"
        return "CENTER"
    if len(pattern) == 6:
        if dashes[1] <= dot and dashes[2] <= dot:
            return "DIVIDE"
        return "PHANTOM"
    return "PDF_DASH"


def _diagnose(context: _Context, **kwargs: Any) -> None:
    context.diagnostics.append(ImportDiagnostic(**kwargs))


# ----------------------------------------------------------------------- paths


@dataclass(slots=True)
class _Subpath:
    start: Point
    items: list[tuple[Any, ...]]  # ("L", p) | ("C", c1, c2, p), points in IR mm
    closed: bool


def _subpaths(
    segments: Sequence[tuple[int, float, float, bool]], m: Matrix
) -> list[_Subpath]:
    result: list[_Subpath] = []
    current: _Subpath | None = None
    pending: list[Point] = []
    for seg_type, x, y, close in segments:
        p = _apply(m, x, y)
        if seg_type == _SEG_MOVETO:
            current = _Subpath(p, [], False)
            result.append(current)
            pending = []
        elif current is None:
            # A path that starts without moveto: PDFium does not emit this, but be safe.
            current = _Subpath(p, [], False)
            result.append(current)
        elif seg_type == _SEG_BEZIERTO:
            pending.append(p)
            if len(pending) == 3:
                current.items.append(("C", pending[0], pending[1], pending[2]))
                pending = []
        else:
            if pending:  # malformed curve: keep its points as lines
                for q in pending:
                    current.items.append(("L", q))
                pending = []
            current.items.append(("L", p))
        if close and current is not None:
            current.closed = True
    return [sp for sp in result if sp.items]


def _collinear(points: Sequence[Point], tol: float) -> bool:
    a, b = points[0], points[-1]
    length = _dist(a, b)
    if length < _EPS:
        return all(_dist(a, p) <= tol for p in points)
    tol = min(tol, 0.05 * length)
    return all(
        abs(_cross((b[0] - a[0], b[1] - a[1]), (p[0] - a[0], p[1] - a[1]))) / length
        <= tol
        for p in points
    )


def _point_from_tiny_shape(sp: _Subpath) -> Point | None:
    """Centre of a closed stroked shape too small to be anything but a point marker.

    CAD plotters draw point entities as tiny circles or polygons; a closed subpath
    whose extent is at most ``POINT_SHAPE_MAX_MM`` in both directions is one.
    """
    if not sp.closed:
        return None
    points = [sp.start] + [item[-1] for item in sp.items]  # on-curve points only
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if max(max(xs) - min(xs), max(ys) - min(ys)) > POINT_SHAPE_MAX_MM + 1e-9:
        return None
    return ((max(xs) + min(xs)) / 2.0, (max(ys) + min(ys)) / 2.0)


def _stroke_items(sp: _Subpath, tol: float, context: _Context) -> list[tuple[Any, ...]]:
    """Path items (``L``/``A``/``B``) of one stroked subpath, arcs recognised."""
    items: list[tuple[Any, ...]] = []
    current = sp.start
    for item in sp.items:
        if item[0] == "L":
            if _dist(current, item[1]) > _EPS:
                items.append(("L", current, item[1]))
            current = item[1]
            continue
        _, c1, c2, p = item
        arc = _bezier_arc(current, c1, c2, p, tol)
        if arc is not None:
            items.append(("A", arc))
            context.note("bezier_arcs")
        elif _collinear((current, c1, c2, p), tol):
            if _dist(current, p) > _EPS:
                items.append(("L", current, p))
        else:
            items.append(("B", (current, c1, c2, p)))
        current = p
    if sp.closed and _dist(current, sp.start) > _EPS:
        items.append(("L", current, sp.start))
    merged: list[tuple[Any, ...]] = []
    for item in items:
        if item[0] == "A" and merged and merged[-1][0] == "A":
            joined = _merge_arcs(merged[-1][1], item[1], tol)
            if joined is not None:
                merged[-1] = ("A", joined)
                continue
        merged.append(item)
    if all(item[0] == "L" for item in merged) and len(merged) >= 6:
        points = [merged[0][1]] + [item[2] for item in merged]
        refit = _refit_polyline(points, sp.closed, tol)
        arcs = sum(1 for item in refit if item[0] == "A")
        if arcs:
            context.note("polyline_arc_refits", arcs)
            return refit
    return merged


def _arc_entity(arc: _Arc, common: dict[str, Any]) -> dict[str, Any]:
    if abs(arc.sweep) >= _TWO_PI - 1e-6:
        return {
            **common,
            "kind": "CIRCLE",
            "center": [arc.center[0], arc.center[1]],
            "radius": arc.radius,
        }
    start, end = (arc.start, arc.end) if arc.ccw else (arc.end, arc.start)
    return {
        **common,
        "kind": "ARC",
        "center": [arc.center[0], arc.center[1]],
        "radius": arc.radius,
        "start_angle": _norm_deg(math.degrees(start)),
        "end_angle": _norm_deg(math.degrees(end)),
    }


def _spline_entity(
    beziers: Sequence[tuple[Point, Point, Point, Point]], common: dict[str, Any]
) -> dict[str, Any]:
    control: list[list[float]] = [[beziers[0][0][0], beziers[0][0][1]]]
    for _p0, c1, c2, p3 in beziers:
        control.extend([[c1[0], c1[1]], [c2[0], c2[1]], [p3[0], p3[1]]])
    n = len(beziers)
    knots = [0.0] * 4
    for i in range(1, n):
        knots.extend([float(i)] * 3)
    knots.extend([float(n)] * 4)
    return {
        **common,
        "kind": "SPLINE",
        "degree": 3,
        "control_points": control,
        "knots": knots,
    }


def _emit_stroke(
    items: Sequence[tuple[Any, ...]],
    closed: bool,
    common: dict[str, Any],
    context: _Context,
) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    all_lines = all(item[0] == "L" for item in items)
    i = 0
    while i < len(items):
        kind = items[i][0]
        if kind == "L":
            j = i + 1
            while (
                j < len(items)
                and items[j][0] == "L"
                and _dist(items[j][1], items[j - 1][2]) <= _EPS
            ):
                j += 1
            run = items[i:j]
            if len(run) >= 3:
                vertices = [[run[0][1][0], run[0][1][1]]] + [
                    [s[2][0], s[2][1]] for s in run
                ]
                entity = {**common, "id": context.allocate_id(), "kind": "LWPOLYLINE"}
                if all_lines and closed and len(run) == len(items):
                    vertices.pop()
                    entity["closed"] = True
                entity["vertices"] = vertices
                entities.append(entity)
            else:
                for _, a, b in run:
                    entities.append(
                        {
                            **common,
                            "id": context.allocate_id(),
                            "kind": "LINE",
                            "p1": [a[0], a[1]],
                            "p2": [b[0], b[1]],
                        }
                    )
            i = j
        elif kind == "A":
            entities.append(
                _arc_entity(items[i][1], {**common, "id": context.allocate_id()})
            )
            i += 1
        else:
            j = i + 1
            while (
                j < len(items)
                and items[j][0] == "B"
                and _dist(items[j][1][0], items[j - 1][1][3]) <= _EPS
            ):
                j += 1
            entities.append(
                _spline_entity(
                    [item[1] for item in items[i:j]],
                    {**common, "id": context.allocate_id()},
                )
            )
            context.note("splines")
            i = j
    return entities


def _loop_points(sp: _Subpath, tol: float) -> tuple[list[Point], bool]:
    points = [sp.start]
    current = sp.start
    curved = False
    for item in sp.items:
        if item[0] == "L":
            points.append(item[1])
            current = item[1]
        else:
            _flatten_bezier(current, item[1], item[2], item[3], tol, points)
            current = item[3]
            curved = True
    cleaned: list[Point] = []
    for p in points:
        if not cleaned or _dist(cleaned[-1], p) > _EPS:
            cleaned.append(p)
    if len(cleaned) > 1 and _dist(cleaned[0], cleaned[-1]) <= _EPS:
        cleaned.pop()
    return cleaned, curved


def _thin_rectangle_line(points: Sequence[Point]) -> tuple[Point, Point, float] | None:
    if len(points) != 4:
        return None
    edges = [
        (points[(i + 1) % 4][0] - points[i][0], points[(i + 1) % 4][1] - points[i][1])
        for i in range(4)
    ]
    lengths = [math.hypot(*e) for e in edges]
    if min(lengths) < _EPS:
        return None
    for i in range(4):
        a, b = edges[i], edges[(i + 1) % 4]
        if abs(a[0] * b[0] + a[1] * b[1]) > 0.05 * lengths[i] * lengths[(i + 1) % 4]:
            return None
    if (
        abs(lengths[0] - lengths[2]) > 0.05 * lengths[0]
        or abs(lengths[1] - lengths[3]) > 0.05 * lengths[1]
    ):
        return None
    short_first = lengths[0] <= lengths[1]
    short, long_ = (lengths[0], lengths[1]) if short_first else (lengths[1], lengths[0])
    if short > FILL_AS_LINE_MAX_WIDTH_MM or long_ < 4.0 * short:
        return None
    if short_first:
        a = ((points[0][0] + points[1][0]) / 2.0, (points[0][1] + points[1][1]) / 2.0)
        b = ((points[2][0] + points[3][0]) / 2.0, (points[2][1] + points[3][1]) / 2.0)
    else:
        a = ((points[3][0] + points[0][0]) / 2.0, (points[3][1] + points[0][1]) / 2.0)
        b = ((points[1][0] + points[2][0]) / 2.0, (points[1][1] + points[2][1]) / 2.0)
    return a, b, short


def _fill_entities(
    subpaths: Sequence[_Subpath],
    common: dict[str, Any],
    fill_color: str | None,
    context: _Context,
) -> list[dict[str, Any]]:
    loops: list[list[Point]] = []
    curved = False
    for sp in subpaths:
        points, sp_curved = _loop_points(sp, FLATTEN_TOLERANCE_MM)
        curved = curved or sp_curved
        if len(points) >= 3:
            loops.append(points)
    if not loops:
        return []
    if len(loops) == 1 and not curved:
        thin = _thin_rectangle_line(loops[0])
        if thin is not None:
            a, b, width = thin
            context.note("fill_as_line")
            entity = {
                **common,
                "id": context.allocate_id(),
                "kind": "LINE",
                "p1": [a[0], a[1]],
                "p2": [b[0], b[1]],
                "lineweight_mm": round(width, 4),
            }
            if fill_color is not None:
                entity["color"] = fill_color
            return [entity]
    xs = [p[0] for loop in loops for p in loop]
    ys = [p[1] for loop in loops for p in loop]
    if curved:
        context.note("hatch_curves_flattened")
        if max(xs) - min(xs) <= 10.0 and max(ys) - min(ys) <= 10.0:
            context.note("outline_glyph_candidates")
    entity = {
        **common,
        "id": context.allocate_id(),
        "kind": "HATCH",
        "solid": True,
        "pattern": "SOLID",
        "loops": [
            {"vertices": [[p[0], p[1]] for p in loop], "is_outer": index == 0}
            for index, loop in enumerate(loops)
        ],
    }
    if fill_color is not None:
        entity["color"] = fill_color
    return [entity]


# ------------------------------------------------------------------------ clips


@dataclass(slots=True)
class _Clip:
    rect: tuple[float, float, float, float]
    exact: bool


def _clip_of(raw: Any, obj: Any, chain: Matrix, frame: _PageFrame) -> _Clip | None:
    paths = _clip_segments(raw, obj)
    if not paths:
        return None
    rect: list[float] | None = None
    exact = True
    for segs in paths:
        points = [_apply(chain, x, y) for _t, x, y in segs]
        has_curve = any(t == _SEG_BEZIERTO for t, _x, _y in segs)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        box = [min(xs), min(ys), max(xs), max(ys)]
        if has_curve or not _is_axis_rectangle(points):
            exact = False
        if rect is None:
            rect = box
        else:
            rect = [
                max(rect[0], box[0]),
                max(rect[1], box[1]),
                min(rect[2], box[2]),
                min(rect[3], box[3]),
            ]
    if rect is None:
        return None
    px0, py0, px1, py1 = frame.rect
    if (
        rect[0] <= px0 + _EPS
        and rect[1] <= py0 + _EPS
        and rect[2] >= px1 - _EPS
        and rect[3] >= py1 - _EPS
    ):
        return None
    return _Clip((rect[0], rect[1], rect[2], rect[3]), exact)


def _is_axis_rectangle(points: Sequence[Point]) -> bool:
    distinct: list[Point] = []
    for p in points:
        if not any(_dist(p, q) <= 1e-6 for q in distinct):
            distinct.append(p)
    if len(distinct) != 4:
        return False
    xs = sorted({round(p[0], 6) for p in distinct})
    ys = sorted({round(p[1], 6) for p in distinct})
    return len(xs) == 2 and len(ys) == 2


def _entity_bbox(entity: dict[str, Any]) -> tuple[float, float, float, float] | None:
    kind = entity["kind"]
    points: list[Point] = []
    if kind == "LINE":
        points = [tuple(entity["p1"]), tuple(entity["p2"])]
    elif kind == "POINT":
        points = [tuple(entity["position"])]
    elif kind == "LWPOLYLINE":
        points = [(v[0], v[1]) for v in entity["vertices"]]
    elif kind == "SPLINE":
        points = [(p[0], p[1]) for p in entity["control_points"]]
    elif kind == "HATCH":
        points = [(v[0], v[1]) for loop in entity["loops"] for v in loop["vertices"]]
    elif kind == "CIRCLE":
        cx, cy = entity["center"]
        r = entity["radius"]
        return (cx - r, cy - r, cx + r, cy + r)
    elif kind == "ARC":
        cx, cy = entity["center"]
        r = entity["radius"]
        start = math.radians(entity["start_angle"])
        sweep = (math.radians(entity["end_angle"]) - start) % _TWO_PI
        points = [
            (cx + r * math.cos(start), cy + r * math.sin(start)),
            (cx + r * math.cos(start + sweep), cy + r * math.sin(start + sweep)),
        ]
        for k in range(4):
            angle = k * math.pi / 2.0
            if (angle - start) % _TWO_PI <= sweep:
                points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    elif kind == "IMAGE":
        x, y = entity["insert"]
        ang = math.radians(entity.get("rotation", 0.0))
        w, h = entity["width"], entity["height"]
        u = (math.cos(ang), math.sin(ang))
        v = (-u[1], u[0])
        points = [
            (x, y),
            (x + u[0] * w, y + u[1] * w),
            (x + u[0] * w + v[0] * h, y + u[1] * w + v[1] * h),
            (x + v[0] * h, y + v[1] * h),
        ]
    elif kind == "TEXT":
        x, y = entity["insert"]
        ang = math.radians(entity.get("rotation", 0.0))
        h = entity["height"]
        w = 0.8 * h * max(1, len(entity.get("text", "")))
        u = (math.cos(ang), math.sin(ang))
        v = (-u[1], u[0])
        points = [
            (x, y),
            (x + u[0] * w, y + u[1] * w),
            (x + u[0] * w + v[0] * h, y + u[1] * w + v[1] * h),
            (x + v[0] * h, y + v[1] * h),
        ]
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _clip_segment(
    a: Point, b: Point, rect: tuple[float, float, float, float]
) -> tuple[Point, Point] | None:
    """Liang-Barsky clip of segment ``ab`` against ``rect``."""
    x0, y0, x1, y1 = rect
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if abs(p) < _EPS:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    if t1 - t0 <= _EPS:
        return None
    return (a[0] + t0 * dx, a[1] + t0 * dy), (a[0] + t1 * dx, a[1] + t1 * dy)


def _inside_rect(
    p: Point, rect: tuple[float, float, float, float], tol: float = 1e-6
) -> bool:
    return (
        rect[0] - tol <= p[0] <= rect[2] + tol
        and rect[1] - tol <= p[1] <= rect[3] + tol
    )


def _clip_arc_entity(
    entity: dict[str, Any], rect: tuple[float, float, float, float], context: _Context
) -> list[dict[str, Any]]:
    cx, cy = entity["center"]
    r = float(entity["radius"])
    if entity["kind"] == "CIRCLE":
        start, sweep = 0.0, _TWO_PI
    else:
        start = math.radians(entity["start_angle"])
        sweep = (math.radians(entity["end_angle"]) - start) % _TWO_PI
        if sweep < _EPS:
            sweep = _TWO_PI
    cuts = {0.0, sweep}
    for x in (rect[0], rect[2]):
        dx = x - cx
        if abs(dx) < r:
            dy = math.sqrt(r * r - dx * dx)
            for y in (cy + dy, cy - dy):
                cuts.add((math.atan2(y - cy, dx) - start) % _TWO_PI)
    for y in (rect[1], rect[3]):
        dy = y - cy
        if abs(dy) < r:
            dx = math.sqrt(r * r - dy * dy)
            for x in (cx + dx, cx - dx):
                cuts.add((math.atan2(dy, x - cx) - start) % _TWO_PI)
    params = sorted(t for t in cuts if t <= sweep + _EPS)
    pieces: list[dict[str, Any]] = []
    for t0, t1 in zip(params, params[1:]):
        if t1 - t0 <= 1e-7:
            continue
        mid = start + (t0 + t1) / 2.0
        if not _inside_rect((cx + r * math.cos(mid), cy + r * math.sin(mid)), rect):
            continue
        piece = {
            k: v for k, v in entity.items() if k not in ("start_angle", "end_angle")
        }
        if pieces:
            piece["id"] = context.allocate_id()
        if t1 - t0 >= _TWO_PI - 1e-6:
            piece["kind"] = "CIRCLE"
        else:
            piece["kind"] = "ARC"
            piece["start_angle"] = _norm_deg(math.degrees(start + t0))
            piece["end_angle"] = _norm_deg(math.degrees(start + t1))
        pieces.append(piece)
    if (
        entity["kind"] == "CIRCLE"
        and len(pieces) >= 2
        and pieces[0]["kind"] == "ARC"
        and pieces[-1]["kind"] == "ARC"
        and abs(math.radians(pieces[0]["start_angle"]) - start % _TWO_PI) % _TWO_PI
        < 1e-7
        and abs(math.radians(pieces[-1]["end_angle"]) - start % _TWO_PI) % _TWO_PI
        < 1e-7
    ):
        # The circle was cut at its parametric origin: the first and last kept
        # pieces are one arc that straddles that origin.
        last = pieces.pop()
        pieces[0]["start_angle"] = last["start_angle"]
    return pieces


def _apply_clip(
    entities: list[dict[str, Any]], clip: _Clip, context: _Context
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    rect = clip.rect
    for entity in entities:
        box = _entity_bbox(entity)
        if box is None:
            result.append(entity)
            continue
        if (
            box[0] >= rect[0] - _EPS
            and box[1] >= rect[1] - _EPS
            and box[2] <= rect[2] + _EPS
            and box[3] <= rect[3] + _EPS
        ):
            result.append(entity)
            continue
        if box[2] < rect[0] or box[0] > rect[2] or box[3] < rect[1] or box[1] > rect[3]:
            context.note("clipped_out")
            continue
        kind = entity["kind"]
        if clip.exact and kind == "LINE":
            piece = _clip_segment(tuple(entity["p1"]), tuple(entity["p2"]), rect)
            if piece is None:
                context.note("clipped_out")
                continue
            context.note("clipped")
            result.append(
                {
                    **entity,
                    "p1": [piece[0][0], piece[0][1]],
                    "p2": [piece[1][0], piece[1][1]],
                }
            )
        elif clip.exact and kind == "LWPOLYLINE":
            vertices = [(v[0], v[1]) for v in entity["vertices"]]
            if entity.get("closed") and len(vertices) > 1:
                vertices.append(vertices[0])
            first = True
            for a, b in zip(vertices, vertices[1:]):
                piece = _clip_segment(a, b, rect)
                if piece is None:
                    continue
                line = {
                    k: v for k, v in entity.items() if k not in ("vertices", "closed")
                }
                line["kind"] = "LINE"
                line["p1"] = [piece[0][0], piece[0][1]]
                line["p2"] = [piece[1][0], piece[1][1]]
                if not first:
                    line["id"] = context.allocate_id()
                first = False
                result.append(line)
            context.note("clipped" if not first else "clipped_out")
        elif clip.exact and kind in ("ARC", "CIRCLE"):
            pieces = _clip_arc_entity(entity, rect, context)
            context.note("clipped" if pieces else "clipped_out")
            result.extend(pieces)
        else:
            context.note("clip_approximated")
            result.append(entity)
    return result


# ------------------------------------------------------------------------ text


@dataclass(slots=True)
class _Char:
    index: int
    ch: str
    origin: Point
    u: Point
    size: float
    width_factor: float
    font: str
    color: str | None
    top: float
    advance_end: float
    obj: int
    layer: str | None


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def _collect_chars(
    raw: Any, textpage: Any, frame: _PageFrame, context: _Context
) -> list[_Char]:
    chars: list[_Char] = []
    count = raw.FPDFText_CountChars(textpage)
    matrix = raw.FS_MATRIX()
    ox, oy = ctypes.c_double(), ctypes.c_double()
    left, right, bottom, top = (ctypes.c_double() for _ in range(4))
    flags = ctypes.c_int()
    font_buf = (ctypes.c_char * 128)()
    loose = raw.FS_RECTF()
    layer_cache: dict[int, str | None] = {}
    color_cache: dict[int, str | None] = {}
    page_scale = _linear_scale(frame.page_map)
    for i in range(count):
        if raw.FPDFText_IsGenerated(textpage, i) == 1:
            continue
        code = raw.FPDFText_GetUnicode(textpage, i)
        if code == 0:
            context.note("text_unmapped")
            continue
        ch = chr(code)
        if ch in "\r\n\t\x00" or ch == "￾":
            continue
        obj = raw.FPDFText_GetTextObject(textpage, i)
        obj_key = int(ctypes.cast(obj, ctypes.c_void_p).value or 0) if obj else 0
        if obj:
            mode = raw.FPDFTextObj_GetTextRenderMode(obj)
            if mode in (3, 7):
                context.note("text_invisible")
                continue
        if not raw.FPDFText_GetMatrix(textpage, i, ctypes.byref(matrix)):
            continue
        a, b, c, d = float(matrix.a), float(matrix.b), float(matrix.c), float(matrix.d)
        ux, uy = _apply_vector(frame.page_map, a, b)
        norm = math.hypot(ux, uy)
        y_scale = math.hypot(c, d)
        if norm < _EPS or y_scale < _EPS:
            continue
        u = (ux / norm, uy / norm)
        v = (-u[1], u[0])
        size = float(raw.FPDFText_GetFontSize(textpage, i)) * y_scale * page_scale
        if size <= _EPS:
            continue
        raw.FPDFText_GetCharOrigin(textpage, i, ctypes.byref(ox), ctypes.byref(oy))
        origin = _apply(frame.page_map, float(ox.value), float(oy.value))
        raw.FPDFText_GetCharBox(
            textpage,
            i,
            ctypes.byref(left),
            ctypes.byref(right),
            ctypes.byref(bottom),
            ctypes.byref(top),
        )
        corners = [
            _apply(frame.page_map, float(left.value), float(bottom.value)),
            _apply(frame.page_map, float(right.value), float(bottom.value)),
            _apply(frame.page_map, float(right.value), float(top.value)),
            _apply(frame.page_map, float(left.value), float(top.value)),
        ]
        rel = [(p[0] - origin[0], p[1] - origin[1]) for p in corners]
        glyph_top = max(r[0] * v[0] + r[1] * v[1] for r in rel)
        advance_end = max(r[0] * u[0] + r[1] * u[1] for r in rel)
        # The tight box is the ink; the loose box is the advance box, which the
        # next glyph's origin follows. Narrow glyphs (punctuation, full-width
        # marks) would otherwise look like word gaps. Both boxes are axis
        # aligned, so only axis-aligned text can use the loose box.
        axis_aligned = abs(u[0] * u[1]) < 0.01
        if axis_aligned and raw.FPDFText_GetLooseCharBox(
            textpage, i, ctypes.byref(loose)
        ):
            loose_corners = [
                _apply(frame.page_map, float(loose.left), float(loose.bottom)),
                _apply(frame.page_map, float(loose.right), float(loose.bottom)),
                _apply(frame.page_map, float(loose.right), float(loose.top)),
                _apply(frame.page_map, float(loose.left), float(loose.top)),
            ]
            loose_end = max(
                (p[0] - origin[0]) * u[0] + (p[1] - origin[1]) * u[1]
                for p in loose_corners
            )
            advance_end = max(advance_end, loose_end)
        if advance_end <= _EPS:
            advance_end = 0.5 * size if ch == " " else 0.0
        length = raw.FPDFText_GetFontInfo(
            textpage, i, font_buf, 128, ctypes.byref(flags)
        )
        font = font_buf.value.decode("utf-8", "replace") if length else ""
        if (
            font.startswith(tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
            and len(font) > 7
            and font[6] == "+"
        ):
            font = font[7:]  # subset prefix
        if obj_key not in layer_cache:
            layer_cache[obj_key] = _ocg_name(raw, obj) if obj else None
            color_cache[obj_key] = (
                _color_hex(_rgba(raw, raw.FPDFPageObj_GetFillColor, obj))
                if obj
                else None
            )
        chars.append(
            _Char(
                index=i,
                ch=ch,
                origin=origin,
                u=u,
                size=size,
                width_factor=math.hypot(a, b) / y_scale,
                font=font,
                color=color_cache[obj_key],
                top=glyph_top,
                advance_end=advance_end,
                obj=obj_key,
                layer=layer_cache[obj_key],
            )
        )
    return chars


def _group_runs(chars: Sequence[_Char]) -> list[list[tuple[_Char, bool]]]:
    """Group characters into text runs; the flag says a space precedes the char."""
    runs: list[list[tuple[_Char, bool]]] = []
    for ch in chars:
        if runs:
            last = runs[-1][-1][0]
            same_dir = last.u[0] * ch.u[0] + last.u[1] * ch.u[1] > 0.9994
            size = max(last.size, ch.size)
            dx, dy = ch.origin[0] - last.origin[0], ch.origin[1] - last.origin[1]
            baseline = abs(-dx * last.u[1] + dy * last.u[0])
            gap = dx * last.u[0] + dy * last.u[1] - last.advance_end
            same_object = last.obj == ch.obj
            # Glyphs of one text object belong together unless they jump (a TJ
            # offset acting as a tab); separately placed glyphs (AutoCAD TrueType
            # output) join across letter gaps and, with a space, across word gaps.
            max_gap = 1.0 * size if same_object else 0.6 * size
            min_gap = -0.6 * size if same_object else -0.3 * size
            if (
                same_dir
                and abs(last.size - ch.size) <= 0.15 * size
                and baseline <= 0.2 * size
                and min_gap <= gap <= max_gap
                and last.font == ch.font
                and last.layer == ch.layer
            ):
                threshold = 0.25 * size if same_object else 0.15 * size
                space = gap > threshold and ch.ch != " " and last.ch != " "
                runs[-1].append((ch, space))
                continue
        runs.append([(ch, False)])
    return runs


def _text_entities(
    chars: Sequence[_Char], frame: _PageFrame, context: _Context
) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for run in _group_runs(chars):
        text = "".join((" " if space else "") + ch.ch for ch, space in run).strip()
        if not text:
            continue
        first = run[0][0]
        glyphs = [ch for ch, _ in run if ch.ch != " "]
        size = sum(ch.size for ch in glyphs) / max(1, len(glyphs))
        rotation = _angle_deg(first.u[0], first.u[1])
        if rotation < 1e-6 or rotation > 360.0 - 1e-6:
            rotation = 0.0
        axis_aligned = min(rotation % 90.0, 90.0 - rotation % 90.0) < 0.5
        if any(_is_cjk(ch.ch) for ch in glyphs):
            height = size
        else:
            caps = [ch.top for ch in glyphs if ch.ch.isdigit() or ch.ch.isupper()]
            if caps and axis_aligned and max(caps) > 0.3 * size:
                height = max(caps)
            else:
                height = 0.72 * size
        entity: dict[str, Any] = {
            "id": context.allocate_id(),
            "kind": "TEXT",
            "layer": context.layer(first.layer),
            "linetype": "CONTINUOUS",
            "insert": [first.origin[0], first.origin[1]],
            "height": round(height, 6),
            "text": text,
            "halign": "left",
            "valign": "baseline",
        }
        if rotation:
            entity["rotation"] = round(rotation, 6)
        if abs(first.width_factor - 1.0) > 0.02:
            entity["width_factor"] = round(first.width_factor, 4)
        if first.font:
            entity["style"] = context.text_style(first.font)
        if first.color is not None:
            entity["color"] = first.color
        if context.options.entity_provenance:
            entity["source"] = {
                "format": "pdf",
                "id": f"{frame.index + 1}:text:{first.index}",
                "kind": "TEXT",
            }
            entity["metadata"] = {
                "pdf": {
                    "page": frame.index + 1,
                    "font": first.font,
                    "font_size_mm": round(size, 6),
                    "chars": len(run),
                }
            }
        entities.append(entity)
        context.note("text_runs")
    return entities


# ---------------------------------------------------------------------- images


def _image_entity(
    pypdfium2: Any,
    raw: Any,
    page: Any,
    obj: Any,
    m: Matrix,
    common: dict[str, Any],
    frame: _PageFrame,
    context: _Context,
) -> dict[str, Any] | None:
    c0, c1, c3 = _apply(m, 0.0, 0.0), _apply(m, 1.0, 0.0), _apply(m, 0.0, 1.0)
    w_vec = (c1[0] - c0[0], c1[1] - c0[1])
    h_vec = (c3[0] - c0[0], c3[1] - c0[1])
    width, height = math.hypot(*w_vec), math.hypot(*h_vec)
    if width <= _EPS or height <= _EPS:
        return None
    context.note("images")
    number = context.counts["images"]
    metadata = raw.FPDF_IMAGEOBJ_METADATA()
    pixel: dict[str, Any] = {}
    if raw.FPDFImageObj_GetImageMetadata(obj, page, ctypes.byref(metadata)):
        pixel = {
            "pixel_width": int(metadata.width),
            "pixel_height": int(metadata.height),
            "bits_per_pixel": int(metadata.bits_per_pixel),
            "colorspace": int(metadata.colorspace),
        }
    entity: dict[str, Any] = {
        **common,
        "id": context.allocate_id(),
        "kind": "IMAGE",
        "name": f"pdf_p{frame.index + 1}_image{number}",
        "insert": [c0[0], c0[1]],
        "width": width,
        "height": height,
    }
    rotation = _angle_deg(w_vec[0], w_vec[1])
    if rotation > 1e-9:
        entity["rotation"] = rotation
    mirrored = _cross(w_vec, h_vec) < 0.0
    embedded = False
    if context.embed_images:
        pixels = pixel.get("pixel_width", 0) * pixel.get("pixel_height", 0)
        if pixels > MAX_EMBED_PIXELS:
            context.note("image_too_large")
        else:
            png = _render_image_png(raw, obj)
            if png is None:
                context.note("image_embed_failed")
            else:
                entity["mime_type"] = "image/png"
                entity["data"] = base64.b64encode(png).decode("ascii")
                embedded = True
    if not embedded:
        context.note("image_placeholder")
    if context.options.entity_provenance:
        meta = dict(entity.get("metadata", {}).get("pdf", {}))
        meta.update(pixel)
        if mirrored:
            meta["mirrored"] = True
        entity["metadata"] = {"pdf": meta}
    return entity


def _render_image_png(raw: Any, obj: Any) -> bytes | None:
    try:
        bitmap = raw.FPDFImageObj_GetBitmap(obj)
    except Exception:  # noqa: BLE001
        return None
    if not bitmap:
        return None
    try:
        fmt = int(raw.FPDFBitmap_GetFormat(bitmap))
        width = int(raw.FPDFBitmap_GetWidth(bitmap))
        height = int(raw.FPDFBitmap_GetHeight(bitmap))
        stride = int(raw.FPDFBitmap_GetStride(bitmap))
        pointer = raw.FPDFBitmap_GetBuffer(bitmap)
        mode = {1: "L", 2: "BGR", 3: "BGRx", 4: "BGRA"}.get(fmt)
        if mode is None or width <= 0 or height <= 0 or not pointer:
            return None
        data = ctypes.string_at(pointer, stride * height)
        return _png_bytes(width, height, mode, data, stride)
    except Exception:  # noqa: BLE001
        return None
    finally:
        raw.FPDFBitmap_Destroy(bitmap)


# ---------------------------------------------------------------------- objects


def _common_fields(
    context: _Context,
    frame: _PageFrame,
    *,
    layer: str | None,
    linetype: str,
    color: str | None,
    lineweight_mm: float | None,
    source_id: str,
    source_kind: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"layer": context.layer(layer), "linetype": linetype}
    if color is not None:
        result["color"] = color
    if lineweight_mm is not None:
        result["lineweight_mm"] = round(lineweight_mm, 4)
    if context.options.entity_provenance:
        result["source"] = {"format": "pdf", "id": source_id, "kind": source_kind}
        meta: dict[str, Any] = {"page": frame.index + 1}
        if layer:
            meta["ocg"] = layer
        if extra:
            meta.update(extra)
        result["metadata"] = {"pdf": meta}
    return result


def _convert_path(
    raw: Any,
    obj: Any,
    m: Matrix,
    chain: Matrix,
    frame: _PageFrame,
    context: _Context,
    source_id: str,
) -> list[dict[str, Any]]:
    fill_mode, stroke_flag = ctypes.c_int(), ctypes.c_int()
    if not raw.FPDFPath_GetDrawMode(
        obj, ctypes.byref(fill_mode), ctypes.byref(stroke_flag)
    ):
        return []
    stroke = bool(stroke_flag.value)
    fill = int(fill_mode.value) != 0
    if not stroke and not fill:
        return []
    subpaths = _subpaths(_segments(raw, obj), m)
    if not subpaths:
        return []
    scale = _linear_scale(m)
    layer = _ocg_name(raw, obj)
    entities: list[dict[str, Any]] = []
    if stroke:
        width = ctypes.c_float()
        raw.FPDFPageObj_GetStrokeWidth(obj, ctypes.byref(width))
        lineweight = max(0.0, float(width.value)) * scale
        dash_count = int(raw.FPDFPageObj_GetDashCount(obj))
        dashes: list[float] = []
        if dash_count > 0:
            array = (ctypes.c_float * dash_count)()
            if raw.FPDFPageObj_GetDashArray(obj, array, dash_count):
                dashes = [float(v) * scale for v in array]
        common = _common_fields(
            context,
            frame,
            layer=layer,
            linetype=context.linetype(dashes),
            color=_color_hex(_rgba(raw, raw.FPDFPageObj_GetStrokeColor, obj)),
            lineweight_mm=lineweight,
            source_id=source_id,
            source_kind="PATH",
        )
        for sp in subpaths:
            point = _point_from_tiny_shape(sp)
            if point is not None:
                context.note("points_from_tiny_shapes")
                entities.append(
                    {
                        **common,
                        "id": context.allocate_id(),
                        "kind": "POINT",
                        "position": [point[0], point[1]],
                    }
                )
                continue
            items = _stroke_items(sp, FIT_TOLERANCE_MM, context)
            entities.extend(_emit_stroke(items, sp.closed, common, context))
    if fill:
        common = _common_fields(
            context,
            frame,
            layer=layer,
            linetype="CONTINUOUS",
            color=None,
            lineweight_mm=None,
            source_id=source_id,
            source_kind="PATH",
            extra={"fill_rule": "evenodd" if int(fill_mode.value) == 1 else "nonzero"},
        )
        entities.extend(
            _fill_entities(
                subpaths,
                common,
                _color_hex(_rgba(raw, raw.FPDFPageObj_GetFillColor, obj)),
                context,
            )
        )
    clip = _clip_of(raw, obj, chain, frame)
    if clip is not None and entities:
        entities = _apply_clip(entities, clip, context)
    return entities


def _walk_objects(
    pypdfium2: Any,
    raw: Any,
    page: Any,
    parent: Any,
    chain: Matrix,
    frame: _PageFrame,
    context: _Context,
    entities: list[dict[str, Any]],
    depth: int,
    prefix: str,
) -> None:
    if parent is None:
        count = raw.FPDFPage_CountObjects(page)
        getter = raw.FPDFPage_GetObject
        handle = page
    else:
        count = raw.FPDFFormObj_CountObjects(parent)
        getter = raw.FPDFFormObj_GetObject
        handle = parent
    for index in range(max(0, int(count))):
        obj = getter(handle, index)
        if not obj:
            continue
        obj_type = int(raw.FPDFPageObj_GetType(obj))
        source_id = f"{prefix}{index}"
        m = _concat(_matrix_of(raw, obj), chain)
        try:
            if obj_type == _OBJ_FORM:
                context.source_counts["FORM"] += 1
                if depth + 1 >= MAX_FORM_DEPTH:
                    context.note("form_depth_exceeded")
                    continue
                _walk_objects(
                    pypdfium2,
                    raw,
                    page,
                    obj,
                    m,
                    frame,
                    context,
                    entities,
                    depth + 1,
                    source_id + "/",
                )
            elif obj_type == _OBJ_PATH:
                context.source_counts["PATH"] += 1
                entities.extend(
                    _convert_path(
                        raw,
                        obj,
                        m,
                        chain,
                        frame,
                        context,
                        f"{frame.index + 1}:{source_id}",
                    )
                )
            elif obj_type == _OBJ_IMAGE:
                context.source_counts["IMAGE"] += 1
                common = _common_fields(
                    context,
                    frame,
                    layer=_ocg_name(raw, obj),
                    linetype="CONTINUOUS",
                    color=None,
                    lineweight_mm=None,
                    source_id=f"{frame.index + 1}:{source_id}",
                    source_kind="IMAGE",
                )
                entity = _image_entity(
                    pypdfium2, raw, page, obj, m, common, frame, context
                )
                if entity is not None:
                    clip = _clip_of(raw, obj, chain, frame)
                    entities.extend(
                        _apply_clip([entity], clip, context) if clip else [entity]
                    )
            elif obj_type == _OBJ_TEXT:
                context.source_counts["TEXT"] += 1
            elif obj_type == _OBJ_SHADING:
                context.source_counts["SHADING"] += 1
                context.note("shading_skipped")
            else:
                context.source_counts["UNKNOWN"] += 1
                context.note("unknown_skipped")
        except Exception as exc:  # noqa: BLE001 - one bad object must not sink the page
            if context.options.strict:
                raise ImporterError(
                    f"Failed to convert PDF object {frame.index + 1}:{source_id}: {exc}"
                ) from exc
            context.note("conversion_failed")
            _diagnose(
                context,
                code="PDF_ENTITY_CONVERSION_FAILED",
                severity="error",
                message=f"Skipped PDF object {frame.index + 1}:{source_id}: {exc}",
                source_id=f"{frame.index + 1}:{source_id}",
                action="skipped",
            )


def _dedupe(entities: list[dict[str, Any]], context: _Context) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for entity in entities:
        kind = entity["kind"]
        key: tuple[Any, ...] | None = None
        if kind == "LINE":
            a = (round(entity["p1"][0], 4), round(entity["p1"][1], 4))
            b = (round(entity["p2"][0], 4), round(entity["p2"][1], 4))
            key = (
                "LINE",
                min(a, b),
                max(a, b),
                entity.get("layer"),
                entity.get("linetype"),
                entity.get("color"),
                entity.get("lineweight_mm"),
            )
        elif kind == "CIRCLE":
            key = (
                "CIRCLE",
                round(entity["center"][0], 4),
                round(entity["center"][1], 4),
                round(entity["radius"], 4),
                entity.get("layer"),
                entity.get("linetype"),
                entity.get("color"),
                entity.get("lineweight_mm"),
            )
        elif kind == "ARC":
            key = (
                "ARC",
                round(entity["center"][0], 4),
                round(entity["center"][1], 4),
                round(entity["radius"], 4),
                round(entity["start_angle"], 3),
                round(entity["end_angle"], 3),
                entity.get("layer"),
                entity.get("linetype"),
                entity.get("color"),
                entity.get("lineweight_mm"),
            )
        if key is not None:
            if key in seen:
                context.note("duplicates_removed")
                continue
            seen.add(key)
        result.append(entity)
    return result


# -------------------------------------------------------------------- document


def _select_pages(pages: Sequence[int] | None, page_count: int) -> list[int]:
    if pages is None:
        return list(range(page_count))
    selected: list[int] = []
    for number in pages:
        if isinstance(number, bool) or not isinstance(number, int):
            raise ImporterError(f"pages must contain 1-based integers, got {number!r}")
        if not 1 <= number <= page_count:
            raise ImporterError(
                f"page {number} is out of range; the PDF has {page_count} page(s)"
            )
        if number - 1 not in selected:
            selected.append(number - 1)
    if not selected:
        raise ImporterError("pages must select at least one page")
    return selected


def _meta_text(raw: Any, pdf: Any, tag: str) -> str | None:
    try:
        value = _utf16_buffer(raw.FPDF_GetMetaText, pdf, tag.encode("ascii"))
    except Exception:  # noqa: BLE001
        return None
    return value or None


def _pypdfium2_version(pypdfium2: Any) -> str:
    for name in ("PYPDFIUM_INFO", "V_PYPDFIUM2", "__version__"):
        value = getattr(pypdfium2, name, None)
        if value:
            return str(value)
    return "unknown"


def _pdfium_version(pypdfium2: Any) -> str:
    for name in ("PDFIUM_INFO", "V_LIBPDFIUM"):
        value = getattr(pypdfium2, name, None)
        if value:
            return str(value)
    return "unknown"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def convert_pdf_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
    pages: Sequence[int] | None = None,
    scale: str | float = "auto",
    embed_images: bool = False,
    page_gap_ratio: float = DEFAULT_PAGE_GAP_RATIO,
) -> ImportResult:
    """Read a vector PDF with PDFium and rebuild its CAD structure as IR.

    ``pages`` selects 1-based page numbers (default: all pages, tiled along +X).
    ``scale`` is ``"auto"`` (recover the plot scale from dimension texts and
    scale to model millimetres when enough dimensions agree), ``"none"`` (keep
    paper millimetres) or a number of model millimetres per paper millimetre.
    ``embed_images`` stores raster images as PNG data in ``IMAGE`` entities.
    """
    pypdfium2, _raw = _load_pdfium()
    source_path = Path(path)
    try:
        pdf = pypdfium2.PdfDocument(str(source_path))
    except Exception as exc:
        raise ImporterError(f"Failed to read PDF file {source_path}: {exc}") from exc
    try:
        return pdf_document_to_ir(
            pdf,
            source_name=source_path.name,
            source_sha256=_sha256_file(source_path),
            options=options,
            pages=pages,
            scale=scale,
            embed_images=embed_images,
            page_gap_ratio=page_gap_ratio,
        )
    finally:
        try:
            pdf.close()
        except Exception:  # noqa: BLE001
            pass


def pdf_document_to_ir(
    pdf: Any,
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    options: ImportOptions | None = None,
    pages: Sequence[int] | None = None,
    scale: str | float = "auto",
    embed_images: bool = False,
    page_gap_ratio: float = DEFAULT_PAGE_GAP_RATIO,
) -> ImportResult:
    """Convert an open :class:`pypdfium2.PdfDocument` to IR (see the module doc)."""
    pypdfium2, raw = _load_pdfium()
    import_options = options or ImportOptions()
    if isinstance(scale, str):
        if scale not in ("auto", "none"):
            raise ImporterError("scale must be 'auto', 'none' or a positive number")
    elif (
        isinstance(scale, bool) or not isinstance(scale, (int, float)) or not scale > 0
    ):
        raise ImporterError("scale must be 'auto', 'none' or a positive number")
    if not isinstance(page_gap_ratio, (int, float)) or page_gap_ratio < 0:
        raise ImporterError("page_gap_ratio must be a non-negative number")
    context = _Context(options=import_options, embed_images=embed_images)
    try:
        page_count = len(pdf)
    except Exception as exc:
        raise ImporterError(f"Failed to read PDF pages: {exc}") from exc
    selected = _select_pages(pages, page_count)

    sizes: list[tuple[float, float]] = []
    for index in selected:
        page = pdf[index]
        width, height = page.get_size()
        sizes.append((float(width) * PT_TO_MM, float(height) * PT_TO_MM))
    gap = (
        max(w for w, _h in sizes) * float(page_gap_ratio) if len(selected) > 1 else 0.0
    )

    entities: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []
    frames: list[_PageFrame] = []
    offset_x = 0.0
    annotations = 0
    for position, index in enumerate(selected):
        page = pdf[index]
        frame = _page_frame(page, index, offset_x)
        frames.append(frame)
        before = len(entities)
        counts_before = Counter(context.source_counts)
        try:
            _walk_objects(
                pypdfium2,
                raw,
                page,
                None,
                frame.page_map,
                frame,
                context,
                entities,
                0,
                "",
            )
            textpage = page.get_textpage()
            try:
                entities.extend(
                    _text_entities(
                        _collect_chars(raw, textpage, frame, context), frame, context
                    )
                )
            finally:
                try:
                    textpage.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                annotations += max(0, int(raw.FPDFPage_GetAnnotCount(page)))
            except Exception:  # noqa: BLE001
                pass
        except ImporterError:
            raise
        except Exception as exc:
            raise ImporterError(f"Failed to read PDF page {index + 1}: {exc}") from exc
        page_objects = {
            k: v - counts_before.get(k, 0)
            for k, v in context.source_counts.items()
            if v - counts_before.get(k, 0)
        }
        page_records.append(
            {
                "index": index + 1,
                "size_mm": [round(frame.width_mm, 4), round(frame.height_mm, 4)],
                "rotation": frame.rotation,
                "offset_mm": [round(offset_x, 4), 0.0],
                "objects": dict(sorted(page_objects.items())),
                "entity_count": len(entities) - before,
            }
        )
        offset_x += sizes[position][0] + gap
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass

    entities = _dedupe(entities, context)

    # Paper scale
    factor: float | None = None
    scale_record: dict[str, Any] = {
        "mode": scale if isinstance(scale, str) else "explicit"
    }
    if scale == "auto":
        texts = [e for e in entities if e["kind"] == "TEXT"]
        estimate = paper_scale.estimate_scale(entities, texts)
        scale_record.update(estimate.to_json())
        if estimate.mm_per_unit is not None:
            label = paper_scale.snap_paper_scale(estimate.mm_per_unit)
            scale_record["label"] = label
            if estimate.confident:
                factor = paper_scale.paper_scale_value(label) or estimate.mm_per_unit
    elif not isinstance(scale, str):
        factor = float(scale)
        scale_record["label"] = paper_scale.snap_paper_scale(factor)
    if factor is not None and abs(factor - 1.0) <= 1e-9:
        factor = None
    if factor is not None:
        entities = [paper_scale.scale_entity(e, factor) for e in entities]
        scale_record["applied"] = factor

    bbox_min = [frames[0].offset_x, 0.0]
    bbox_max = [
        frames[-1].offset_x + frames[-1].width_mm,
        max(f.height_mm for f in frames),
    ]
    if factor is not None:
        bbox_min = [v * factor for v in bbox_min]
        bbox_max = [v * factor for v in bbox_max]

    _append_diagnostics(context, frames, annotations, scale_record, factor)

    file_version = ctypes.c_int()
    version = None
    try:
        if raw.FPDF_GetFileVersion(pdf, ctypes.byref(file_version)):
            version = f"{file_version.value // 10}.{file_version.value % 10}"
    except Exception:  # noqa: BLE001
        version = None
    source: dict[str, Any] = {
        "format": "pdf",
        "version": version or "unknown",
        "metadata": {
            "pdf": {
                "parser_version": _pypdfium2_version(pypdfium2),
                "pdfium_version": _pdfium_version(pypdfium2),
                "page_count": page_count,
                "pages": page_records,
                "producer": _meta_text(raw, pdf, "Producer"),
                "creator": _meta_text(raw, pdf, "Creator"),
                "title": _meta_text(raw, pdf, "Title"),
            }
        },
    }
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256
    header: dict[str, Any] = {
        "units": "mm",
        "angle_unit": "deg",
        "coord_space": "world",
        "bbox": {"min": bbox_min, "max": bbox_max},
        "metadata": {
            "pdf": {
                "coordinate_space": "model" if factor is not None else "paper",
                "paper_scale": scale_record,
                "tiled_pages": len(frames) > 1,
                "page_gap_mm": gap,
            }
        },
    }
    if factor is not None:
        header["linetype_scale"] = factor
    document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "header": header,
        "source": source,
        "tables": {
            "layers": context.layers,
            "linetypes": context.linetypes,
            "text_styles": context.text_styles,
        },
        "entities": entities,
    }
    if import_options.validate:
        validate_ir(document)

    converted = Counter(str(e["kind"]) for e in entities)
    skipped = {
        key: context.counts[key]
        for key in (
            "shading_skipped",
            "unknown_skipped",
            "clipped_out",
            "text_invisible",
            "text_unmapped",
            "conversion_failed",
            "duplicates_removed",
            "form_depth_exceeded",
        )
        if context.counts[key]
    }
    statistics: dict[str, Any] = {
        "source_format": "pdf",
        "source_pages": page_count,
        "converted_pages": len(selected),
        "source_entities": sum(context.source_counts.values()),
        "source_entity_counts": dict(sorted(context.source_counts.items())),
        "converted_entities": len(entities),
        "converted_entity_counts": dict(sorted(converted.items())),
        "skipped_entities": sum(skipped.values()),
        "skipped_entity_counts": skipped,
        "approximated_entities": context.counts["hatch_curves_flattened"]
        + context.counts["clip_approximated"],
        "bezier_arcs": context.counts["bezier_arcs"],
        "polyline_arc_refits": context.counts["polyline_arc_refits"],
        "splines": context.counts["splines"],
        "fill_as_line": context.counts["fill_as_line"],
        "points_from_tiny_shapes": context.counts["points_from_tiny_shapes"],
        "text_runs": context.counts["text_runs"],
        "images": context.counts["images"],
        "annotations": annotations,
        "paper_scale": scale_record,
    }
    return ImportResult(
        document=document, diagnostics=context.diagnostics, statistics=statistics
    )


def _append_diagnostics(
    context: _Context,
    frames: Sequence[_PageFrame],
    annotations: int,
    scale_record: dict[str, Any],
    factor: float | None,
) -> None:
    counts = context.counts
    if len(frames) > 1:
        _diagnose(
            context,
            code="PDF_PAGES_TILED",
            severity="warning",
            message=f"Placed {len(frames)} PDF pages side by side in one IR modelspace; page offsets remain in source metadata.",
            action="flattened",
            details={
                "page_count": len(frames),
                "offsets_mm": [[round(f.offset_x, 4), 0.0] for f in frames],
            },
        )
    rotated = [f.index + 1 for f in frames if f.rotation]
    if rotated:
        _diagnose(
            context,
            code="PDF_PAGE_ROTATED",
            severity="info",
            message="Applied the page /Rotate so coordinates follow the page as displayed.",
            action="normalized",
            details={"pages": rotated},
        )
    if any(layer != "0" for layer in context.layers):
        _diagnose(
            context,
            code="PDF_LAYERS_FROM_OCG",
            severity="info",
            message="Layers were taken from optional content group names; objects without a group are on layer 0.",
            action="detected",
            details={
                "layers": sorted(layer for layer in context.layers if layer != "0")
            },
        )
    if counts["polyline_arc_refits"]:
        _diagnose(
            context,
            code="PDF_POLYLINE_ARC_REFITTED",
            severity="info",
            message=f"Replaced {counts['polyline_arc_refits']} evenly turning polyline run(s) by arcs or circles.",
            action="normalized",
            details={"count": counts["polyline_arc_refits"]},
        )
    if counts["points_from_tiny_shapes"]:
        _diagnose(
            context,
            code="PDF_POINT_FROM_TINY_SHAPE",
            severity="info",
            message=(
                f"Read {counts['points_from_tiny_shapes']} closed shape(s) smaller than "
                f"{POINT_SHAPE_MAX_MM} mm as point markers."
            ),
            action="normalized",
            details={
                "count": counts["points_from_tiny_shapes"],
                "max_mm": POINT_SHAPE_MAX_MM,
            },
        )
    if counts["fill_as_line"]:
        _diagnose(
            context,
            code="PDF_FILL_AS_LINE",
            severity="info",
            message=f"Read {counts['fill_as_line']} thin filled rectangle(s) as lines with the rectangle width as lineweight.",
            action="normalized",
            details={"count": counts["fill_as_line"]},
        )
    if counts["hatch_curves_flattened"]:
        _diagnose(
            context,
            code="PDF_HATCH_FLATTENED",
            severity="warning",
            message=f"Flattened curved boundaries of {counts['hatch_curves_flattened']} filled shape(s) into polygon hatch loops.",
            action="approximated",
            details={
                "count": counts["hatch_curves_flattened"],
                "tolerance_mm": FLATTEN_TOLERANCE_MM,
            },
        )
    outlines = counts["outline_glyph_candidates"]
    if outlines >= 20 and outlines > 5 * counts["text_runs"]:
        _diagnose(
            context,
            code="PDF_TEXT_AS_OUTLINES",
            severity="warning",
            message=f"{outlines} small filled curve shapes look like text converted to outlines; they stay as hatches and cannot be read as text without OCR.",
            action="flattened",
            details={"count": outlines},
        )
    if counts["clip_approximated"]:
        _diagnose(
            context,
            code="PDF_CLIP_APPROXIMATED",
            severity="warning",
            message=f"{counts['clip_approximated']} entities cross a non-rectangular or unsupported clip and were kept whole.",
            action="approximated",
            details={"count": counts["clip_approximated"]},
        )
    if counts["clipped_out"]:
        _diagnose(
            context,
            code="PDF_CLIPPED_OUT",
            severity="info",
            message=f"Dropped {counts['clipped_out']} entities that lie entirely outside their clip.",
            action="skipped",
            details={"count": counts["clipped_out"]},
        )
    if counts["text_invisible"]:
        _diagnose(
            context,
            code="PDF_TEXT_INVISIBLE_SKIPPED",
            severity="info",
            message=f"Skipped {counts['text_invisible']} invisible text character(s) (render mode 3, typically an OCR layer).",
            action="skipped",
            details={"count": counts["text_invisible"]},
        )
    if counts["text_unmapped"]:
        _diagnose(
            context,
            code="PDF_TEXT_UNMAPPED_SKIPPED",
            severity="warning",
            message=f"Skipped {counts['text_unmapped']} text character(s) without a Unicode mapping.",
            action="skipped",
            details={"count": counts["text_unmapped"]},
        )
    if counts["image_placeholder"]:
        _diagnose(
            context,
            code="PDF_IMAGE_PLACEHOLDER",
            severity="warning",
            message=f"{counts['image_placeholder']} raster image(s) keep their placement only; pass embed_images=True to embed the pixels.",
            action="preserved_metadata",
            details={"count": counts["image_placeholder"]},
        )
    if counts["image_too_large"]:
        _diagnose(
            context,
            code="PDF_IMAGE_TOO_LARGE",
            severity="warning",
            message=f"{counts['image_too_large']} raster image(s) exceed {MAX_EMBED_PIXELS} pixels and were not embedded.",
            action="preserved_metadata",
            details={"count": counts["image_too_large"]},
        )
    if counts["image_embed_failed"]:
        _diagnose(
            context,
            code="PDF_IMAGE_EMBED_FAILED",
            severity="warning",
            message=f"{counts['image_embed_failed']} raster image(s) could not be decoded and keep their placement only.",
            action="preserved_metadata",
            details={"count": counts["image_embed_failed"]},
        )
    if counts["shading_skipped"] or counts["unknown_skipped"]:
        _diagnose(
            context,
            code="PDF_UNSUPPORTED_OBJECT",
            severity="warning",
            message=f"Skipped {counts['shading_skipped'] + counts['unknown_skipped']} unsupported page object(s) (shadings).",
            action="skipped",
            details={
                "shadings": counts["shading_skipped"],
                "unknown": counts["unknown_skipped"],
            },
        )
    if counts["form_depth_exceeded"]:
        _diagnose(
            context,
            code="PDF_FORM_DEPTH_EXCEEDED",
            severity="warning",
            message=f"Skipped {counts['form_depth_exceeded']} form XObject(s) nested deeper than {MAX_FORM_DEPTH} levels.",
            action="skipped",
            details={"count": counts["form_depth_exceeded"]},
        )
    if annotations:
        _diagnose(
            context,
            code="PDF_ANNOTATIONS_SKIPPED",
            severity="info",
            message=f"Skipped {annotations} annotation(s); only page content is read.",
            action="skipped",
            details={"count": annotations},
        )
    if counts["duplicates_removed"]:
        _diagnose(
            context,
            code="PDF_DUPLICATE_REMOVED",
            severity="info",
            message=f"Removed {counts['duplicates_removed']} exact duplicate line(s), arc(s) or circle(s).",
            action="skipped",
            details={"count": counts["duplicates_removed"]},
        )
    if scale_record.get("mode") == "auto":
        if factor is not None:
            _diagnose(
                context,
                code="PDF_SCALE_ESTIMATED",
                severity="info",
                message=(
                    f"Scaled the drawing from paper to model millimetres by {scale_record.get('label')} "
                    f"({scale_record.get('support')} dimension texts agree)."
                ),
                action="normalized",
                details=dict(scale_record),
            )
        else:
            _diagnose(
                context,
                code="PDF_SCALE_UNKNOWN",
                severity="info",
                message="Coordinates are paper millimetres; too few dimension texts agreed on a plot scale.",
                details=dict(scale_record),
            )
    elif factor is not None:
        _diagnose(
            context,
            code="PDF_SCALE_APPLIED",
            severity="info",
            message=f"Scaled the drawing from paper to model millimetres by {scale_record.get('label')} as requested.",
            action="normalized",
            details=dict(scale_record),
        )
