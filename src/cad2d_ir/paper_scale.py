"""Paper-space scale recovery from dimension texts.

A drawing read from a plot (a vector PDF, a raster scan) is in paper millimetres.
The numbers written on its dimensions tell how long each dimension really is, and
the extension lines on both sides of the number tell how long it is on paper; the
ratio is the plot scale. This module finds those pairs, picks the ratio most of
them agree on and, when enough of them agree, lets the caller scale the drawing
to model millimetres.

The dimension line itself is not used: chained dimensions often share one
dimension line, while the extension lines stay one pair per dimension. Radius
(``R``), diameter (``φ``) and toleranced values contribute their value only.

All functions work on IR entity dicts (``LINE`` and ``TEXT``) and are unit
agnostic: a ratio is "model millimetres per drawing unit".
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

MIN_SUPPORT = 4
# Of the texts that read as numbers, at least this fraction has to agree. A dense
# drawing with 46 numbers once had 6 of them agree by chance on a wrong scale.
MIN_SUPPORT_FRACTION = 0.25
AGREE_TOL = 0.03
_NUM = re.compile(
    r"^\(?\s*(?:[φΦ⌀∅]|R|SR|S?φ)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(?:mm)?\s*\)?"
    r"(?:\s*[±\+\-][\d.]+(?:/[\-\d.]+)?)?\s*$"
)
_SIN_PARALLEL = math.sin(math.radians(5.0))
_SIN_PERPENDICULAR = math.sin(math.radians(12.0))

STANDARD_SCALES: tuple[float, ...] = (
    1, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 75, 80, 100, 120,
    125, 150, 200, 250, 300, 400, 500, 600, 1000, 1200, 1500, 2000, 2500, 5000,
)  # fmt: skip

_Vec = tuple[float, float]


def numeric_value(text: str) -> float | None:
    """Value of a text that reads as a dimension, else ``None``.

    ``"1,500"``, ``"39.0"``, ``"φ20"``, ``"16.0±0.5"`` and ``"(1500)"`` read;
    ``"2-φ10"`` and ``"M12"`` do not.
    """
    t = text.strip().replace("，", ",").replace("．", ".").replace(" ", "")
    if not t:
        return None
    if re.match(r"^[RSφΦ⌀∅]", t) and re.search(r"[A-Za-z]", t[1:]):
        return None
    m = _NUM.match(t)
    if not m:
        return None
    whole = m.group(1).replace(",", "")
    frac = m.group(2)
    try:
        return float(f"{whole}.{frac}" if frac else whole)
    except ValueError:
        return None


@dataclass
class DimPair:
    """One dimension text with the paper span of its extension lines."""

    text: str
    value: float
    span: float
    center: _Vec
    ratio: float
    p1: _Vec | None = None
    p2: _Vec | None = None
    location: _Vec | None = None
    rotation: float = 0.0
    confidence: float | None = None


@dataclass
class ScaleEstimate:
    """Result of :func:`estimate_scale`."""

    mm_per_unit: float | None
    support: int
    spread: float
    n_texts: int
    pairs: list[DimPair] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        if self.mm_per_unit is None:
            return False
        need = max(MIN_SUPPORT, math.ceil(MIN_SUPPORT_FRACTION * self.n_texts))
        return self.support >= need

    def to_json(self) -> dict[str, Any]:
        return {
            "mm_per_unit": self.mm_per_unit,
            "support": self.support,
            "spread": round(self.spread, 4),
            "n_numeric_texts": self.n_texts,
            "confident": self.confident,
        }


def _dot(a: _Vec, b: _Vec) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _sub(a: _Vec, b: _Vec) -> _Vec:
    return (a[0] - b[0], a[1] - b[1])


def _text_frame(t: dict[str, Any]) -> tuple[_Vec, _Vec, _Vec, float, float]:
    """(centre, along-text unit vector, across unit vector, height, width guess)."""
    h = float(t["height"])
    ang = math.radians(float(t.get("rotation", 0.0) or 0.0))
    u = (math.cos(ang), math.sin(ang))
    v = (-u[1], u[0])
    w = 0.8 * h * max(1, len(str(t.get("text", "")).strip()))
    ins = (float(t["insert"][0]), float(t["insert"][1]))
    center = (
        ins[0] + u[0] * w / 2.0 + v[0] * h / 2.0,
        ins[1] + u[1] * w / 2.0 + v[1] * h / 2.0,
    )
    return center, u, v, h, w


def dimension_pairs(
    entities: list[dict[str, Any]],
    texts: list[dict[str, Any]],
    *,
    dim_class: str | None = None,
) -> tuple[list[DimPair], int]:
    """Measure the extension-line span of every numeric dimension text.

    The dimension line is the nearest line parallel to the text that passes just
    below it (0.2 to 3 text heights) and covers the text centre. Extension lines
    are perpendicular lines ending on that dimension line; the nearest one on
    each side of the text centre bounds the dimension. Returns the pairs and the
    number of texts that read as numbers.
    """
    lines: list[tuple[_Vec, _Vec, _Vec, float]] = []
    for e in entities:
        if e.get("kind") != "LINE":
            continue
        if dim_class and str((e.get("metadata") or {}).get("cls") or "") != dim_class:
            continue
        p1 = (float(e["p1"][0]), float(e["p1"][1]))
        p2 = (float(e["p2"][0]), float(e["p2"][1]))
        d = _sub(p2, p1)
        n = math.hypot(d[0], d[1])
        if n < 1e-9:
            continue
        lines.append((p1, p2, (d[0] / n, d[1] / n), n))
    pairs: list[DimPair] = []
    n_numeric = 0
    for t in texts:
        value = numeric_value(str(t.get("text", "")))
        if value is None or value <= 0:
            continue
        n_numeric += 1
        center, u, v, h, w = _text_frame(t)
        dim_v: float | None = None
        dim_s: tuple[float, float] | None = None
        for p1, p2, dirn, _length in lines:
            if abs(dirn[0] * u[1] - dirn[1] * u[0]) > _SIN_PARALLEL:
                continue
            s1, s2 = _dot(_sub(p1, center), u), _dot(_sub(p2, center), u)
            if min(s1, s2) > 0.5 * w or max(s1, s2) < -0.5 * w:
                continue
            mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
            vv = _dot(_sub(mid, center), v)
            if not (-3.0 * h <= vv <= -0.2 * h):
                continue
            if dim_v is None or vv > dim_v:
                dim_v, dim_s = vv, (min(s1, s2), max(s1, s2))
        if dim_v is None or dim_s is None:
            continue
        best_left: tuple[float, _Vec] | None = None
        best_right: tuple[float, _Vec] | None = None
        for p1, p2, dirn, length in lines:
            if abs(_dot(dirn, u)) > _SIN_PERPENDICULAR or length < 0.8 * h:
                continue
            v1, v2 = _dot(_sub(p1, center), v), _dot(_sub(p2, center), v)
            if min(v1, v2) > dim_v + 1.0 * h or max(v1, v2) < dim_v - 1.0 * h:
                continue
            mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
            s = _dot(_sub(mid, center), u)
            if s < dim_s[0] - 1.0 * h or s > dim_s[1] + 1.0 * h:
                continue
            far = p1 if abs(v1 - dim_v) >= abs(v2 - dim_v) else p2
            if s < -0.3 * h and (best_left is None or s > best_left[0]):
                best_left = (s, far)
            if s > 0.3 * h and (best_right is None or s < best_right[0]):
                best_right = (s, far)
        if best_left is None or best_right is None:
            continue
        span = best_right[0] - best_left[0]
        if span < 0.6 * w:
            continue
        loc = (center[0] + v[0] * dim_v, center[1] + v[1] * dim_v)
        conf = (t.get("metadata") or {}).get("confidence")
        pairs.append(
            DimPair(
                str(t.get("text")),
                value,
                span,
                center,
                value / span,
                p1=best_left[1],
                p2=best_right[1],
                location=loc,
                rotation=float(t.get("rotation", 0.0) or 0.0) % 360.0,
                confidence=float(conf) if conf is not None else None,
            )
        )
    return pairs, n_numeric


def estimate_scale(
    entities: list[dict[str, Any]],
    texts: list[dict[str, Any]],
    *,
    min_support: int = MIN_SUPPORT,
    tol: float = AGREE_TOL,
) -> ScaleEstimate:
    """Pick the ratio the densest cluster (±``tol``) of dimension pairs agrees on."""
    del min_support  # kept for signature compatibility; confidence is a property
    pairs, n_numeric = dimension_pairs(entities, texts)
    if not pairs:
        return ScaleEstimate(None, 0, 0.0, n_numeric, [])
    order = sorted(range(len(pairs)), key=lambda k: pairs[k].ratio)
    logs = [math.log(pairs[k].ratio) for k in order]
    width = math.log1p(tol) * 2.0
    best_lo = best_n = 0
    j = 0
    for i in range(len(logs)):
        while logs[i] - logs[j] > width:
            j += 1
        if i - j + 1 > best_n:
            best_n, best_lo = i - j + 1, j
    chosen = [pairs[k] for k in order[best_lo : best_lo + best_n]]
    ratios = sorted(p.ratio for p in chosen)
    mid = len(ratios) // 2
    median = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2.0
    spread = ratios[-1] / ratios[0] - 1.0 if ratios else 0.0
    return ScaleEstimate(float(median), len(chosen), spread, n_numeric, chosen)


def snap_paper_scale(mm_per_paper_mm: float, tol: float = 0.02) -> str:
    """``"1:50"`` style label, snapped to a standard scale when within ``tol``."""
    if mm_per_paper_mm <= 0:
        return "?"
    if mm_per_paper_mm >= 1.0:
        for d in STANDARD_SCALES:
            if abs(mm_per_paper_mm / d - 1.0) <= tol:
                return f"1:{d:g}"
        return f"1:{mm_per_paper_mm:.1f}"
    inv = 1.0 / mm_per_paper_mm
    for d in STANDARD_SCALES:
        if abs(inv / d - 1.0) <= tol:
            return f"{d:g}:1"
    return f"{inv:.1f}:1"


def paper_scale_value(label: str) -> float | None:
    """Inverse of :func:`snap_paper_scale`: model millimetres per paper millimetre."""
    try:
        a, b = label.split(":")
        return float(b) / float(a)
    except (ValueError, AttributeError):
        return None


def scale_entity(entity: dict[str, Any], factor: float) -> dict[str, Any]:
    """Return a copy of ``entity`` with its geometry multiplied by ``factor``.

    Covers every IR entity kind: points, radii, text heights, polyline vertices
    (bulges are scale free), spline control points, hatch loops, image sizes and
    dimension definition points. ``lineweight_mm`` is paper width and is kept.
    """
    out = dict(entity)

    def pt(p: Any) -> list[float]:
        return [float(p[0]) * factor, float(p[1]) * factor]

    for key in ("p1", "p2", "center", "insert", "position", "anchor"):
        if out.get(key) is not None:
            out[key] = pt(out[key])
    for key in ("radius", "height", "width"):
        if isinstance(out.get(key), (int, float)):
            out[key] = float(out[key]) * factor
    if out.get("major_axis") is not None:
        out["major_axis"] = pt(out["major_axis"])
    if isinstance(out.get("vertices"), list):
        out["vertices"] = [
            [float(v[0]) * factor, float(v[1]) * factor, *v[2:]]
            for v in out["vertices"]
        ]
    if isinstance(out.get("control_points"), list):
        out["control_points"] = [pt(p) for p in out["control_points"]]
    if isinstance(out.get("fit_points"), list):
        out["fit_points"] = [pt(p) for p in out["fit_points"]]
    if isinstance(out.get("loops"), list):
        loops = []
        for loop in out["loops"]:
            loop = dict(loop)
            if isinstance(loop.get("vertices"), list):
                loop["vertices"] = [
                    [float(v[0]) * factor, float(v[1]) * factor, *v[2:]]
                    for v in loop["vertices"]
                ]
            loops.append(loop)
        out["loops"] = loops
    if isinstance(out.get("scale"), (int, float)) and out.get("kind") == "POINT":
        out["scale"] = float(out["scale"]) * factor
    if out.get("kind") == "DIMENSION" and isinstance(out.get("definition"), dict):
        d = dict(out["definition"])
        pts = d.get("points")
        if isinstance(pts, dict):
            d["points"] = {
                k: (pt(v) if v is not None else None) for k, v in pts.items()
            }
        if isinstance(d.get("measurement"), (int, float)):
            d["measurement"] = float(d["measurement"]) * factor
        out["definition"] = d
    return out


def dimension_entities(
    pairs: list[DimPair], *, layer: str = "AI_DIMENSION_AUTO"
) -> list[dict[str, Any]]:
    """Agreeing pairs as ``LINEAR`` ``DIMENSION`` entities in the pairs' coordinates.

    The text is the number as written (not re-measured). The source lines stay,
    because chained dimensions share one dimension line.
    """
    out = []
    for p in pairs:
        if p.p1 is None or p.p2 is None or p.location is None:
            continue
        meta: dict[str, Any] = {"source": "text+geometry", "value": p.value}
        if p.confidence is not None:
            meta["confidence"] = p.confidence
        out.append(
            {
                "kind": "DIMENSION",
                "dim_kind": "LINEAR",
                "layer": layer,
                "definition": {
                    "points": {
                        "p1": [p.p1[0], p.p1[1]],
                        "p2": [p.p2[0], p.p2[1]],
                        "location": [p.location[0], p.location[1]],
                        "text_midpoint": [p.center[0], p.center[1]],
                    },
                    "rotation": p.rotation,
                    "text": p.text,
                },
                "metadata": meta,
            }
        )
    return out
